"""Intraday index option buying on a strategy's signal: a signal F&O algo on intraday candles (5 minutes, 15
minutes...) trades the day (MIS) as the options backtest on those candles does: it reads each finished candle, sells
everything at the exit time, stops for the day after a stoploss or target, and buys again on a later day."""

import numpy as np
import pandas as pd
import pytest

from desk import options as opt
from desk.algo import AlgoError
from desk.scaleout import NONE as NO_STEPS
from tests.test_desk_fno_algos import Clock, journal, setup

pytestmark = pytest.mark.unit


def _five_minute(days=("2026-11-16", "2026-11-17", "2026-11-18", "2026-11-19")):
    """Four sessions of 5-minute NIFTY candles: down for two days, then up (a fast/slow cross on the third)."""
    stamps = [pd.Timestamp(f"{d} 09:15") + pd.Timedelta(minutes=5 * i) for d in days for i in range(75)]
    n = len(stamps)
    closes = np.r_[np.linspace(25600, 24800, 150), np.linspace(24800, 25400, n - 150)]
    return pd.DataFrame({"Open": closes, "High": closes + 5, "Low": closes - 5, "Close": closes, "Volume": 0.0},
                        index=pd.DatetimeIndex(stamps))


CALLS_5M = {"name": "Nifty calls 5m", "kind": "structure", "structure": "custom", "bias": "bull", "underlying": "NIFTY",
            "legs": [{"side": "BUY", "type": "CE", "strikes": 0, "lots": 1, "expiry": "near"}], "expiry_kind": "weekly",
            "schedule": "signal", "interval": "5m", "strategy": "sma_cross", "strategy_params": {"fast": 5, "slow": 20},
            "entry_time": "09:15", "exit_time": "15:00", "lots": 1, "leg_scope": "bought",
            "leg_exits": {"unit": "pct", "stop_loss": 30, "target": 0}, "max_loss": 5000, "mode": "paper"}


def _engine(tmp_path, when, bars):
    clock = Clock(when)
    engine, desk, prices = setup(tmp_path, clock)
    engine.fetch = lambda symbol, interval="1d": bars[bars.index <= pd.Timestamp(clock.now.replace(tzinfo=None))]
    return clock, engine, desk, prices


def test_an_intraday_signal_algo_is_described_and_its_candles_checked(tmp_path):
    _, engine, _, _ = _engine(tmp_path, "2026-11-18 11:02", _five_minute())
    algo = engine.create(CALLS_5M)
    assert algo["interval"] == "5m" and "5 minutes candles, intraday: out by 15:00" in algo["log"][-1]["message"]
    assert engine.create({**CALLS_5M, "name": "Day calls", "interval": "1d"})["interval"] == "1d"
    assert engine.create({**CALLS_5M, "name": "Old calls", "interval": None})["interval"] == "1d"   # saved before
    with pytest.raises(AlgoError, match="Candle size"):
        engine.create({**CALLS_5M, "name": "Bad", "interval": "7m"})
    with pytest.raises(AlgoError, match="Intraday candles are for index option algos"):
        engine.create({"name": "Fut 5m", "kind": "futures", "direction": "long", "underlying": "NIFTY",
                       "strategy": "sma_cross", "interval": "5m", "mode": "paper"})


def test_intraday_calls_buy_on_the_candle_signal_with_mis_and_sell_at_the_exit_time(tmp_path):
    bars = _five_minute()
    clock, engine, desk, _ = _engine(tmp_path, "2026-11-18 11:02", bars)
    algo = engine.create(CALLS_5M)
    engine.set_enabled(algo["id"], True)
    out = engine.evaluate(algo["id"])
    assert out["algo"]["book"] is None and "fresh signal" in out["algo"]["log"][-1]["message"]
    assert out["algo"]["last_signal"]["bar"] == "2026-11-18 10:55"           # the newest finished candle
    engine.get(algo["id"])["ready"] = True
    book = engine.evaluate(algo["id"])["algo"]["book"]
    assert book["legs"][0]["type"] == "CE" and book["legs"][0]["action"] == 1
    assert {r["product"] for r in journal(desk)} == {"MIS"}
    clock.set("2026-11-18 15:00")
    out = engine.evaluate(algo["id"])
    assert out["algo"]["book"] is None and out["algo"]["history"][-1]["reason"] == "exit time 15:00"
    assert out["algo"]["ready"]                                              # buys again tomorrow on the same signal
    clock.set("2026-11-18 15:05")
    assert engine.evaluate(algo["id"])["algo"]["book"] is None              # nothing after the exit time
    # Next morning: yesterday's last candle still says buy, and the backtest buys on it once the trade window opens.
    clock.set("2026-11-19 09:16")
    out = engine.evaluate(algo["id"])
    assert out["algo"]["book"] is None and out["status"] == "Running: waiting for the trade window (09:30-15:00)"
    clock.set("2026-11-19 09:31")
    assert engine.evaluate(algo["id"])["algo"]["book"] is not None


def test_a_stoploss_ends_the_day_and_the_next_day_buys_again(tmp_path):
    bars = _five_minute()
    clock, engine, _, prices = _engine(tmp_path, "2026-11-18 11:02", bars)
    algo = engine.create(CALLS_5M)
    engine.set_enabled(algo["id"], True)
    engine.evaluate(algo["id"])
    engine.get(algo["id"])["ready"] = True
    leg = engine.evaluate(algo["id"])["algo"]["book"]["legs"][0]
    prices.override[leg["instrument"]] = leg["avg"] * 0.5                     # a 50% fall: the 30% stoploss
    clock.set("2026-11-18 11:04")
    out = engine.evaluate(algo["id"])
    assert out["algo"]["book"] is None and out["algo"]["history"][-1]["reason"] == "every leg has exited on its own rules"
    assert "Stoploss on bought leg" in " ".join(x["message"] for x in out["algo"]["log"])
    prices.override.clear()
    clock.set("2026-11-18 11:20")
    out = engine.evaluate(algo["id"])
    assert out["algo"]["book"] is None and out["status"] == "Running: done for today"
    clock.set("2026-11-19 09:31")
    assert engine.evaluate(algo["id"])["algo"]["book"] is not None


def test_stale_intraday_candles_are_not_traded(tmp_path):
    bars = _five_minute()
    clock, engine, _, _ = _engine(tmp_path, "2026-11-18 11:02", bars)
    engine.fetch = lambda symbol, interval="1d": bars[bars.index <= pd.Timestamp("2026-11-18 10:00")]
    algo = engine.create(CALLS_5M)
    engine.set_enabled(algo["id"], True)
    out = engine.evaluate(algo["id"])
    assert out["status"].startswith("Blocked") and "stale" in out["algo"]["log"][-1]["message"]
    # No candle from today half an hour after the open: also stale.
    engine.fetch = lambda symbol, interval="1d": bars[bars.index < pd.Timestamp("2026-11-18")]
    clock.set("2026-11-18 09:50")
    engine.get(algo["id"])["last_signal"] = None
    out = engine.evaluate(algo["id"])
    assert "No NIFTY candle from today" in out["algo"]["log"][-1]["message"]


def test_the_intraday_options_backtest_takes_no_new_trade_after_a_stoploss_that_day():
    days = ("2026-11-16", "2026-11-17")
    stamps = [pd.Timestamp(f"{d} 09:15") + pd.Timedelta(minutes=15 * i) for d in days for i in range(25)]
    closes = np.full(len(stamps), 25000.0)
    closes[3:25] = 24700.0                                   # day one: a fall after the open stops the call out
    bars = pd.DataFrame({"Open": closes, "High": closes + 2, "Low": closes - 2, "Close": closes},
                        index=pd.DatetimeIndex(stamps))
    vix = pd.Series(14.0, index=pd.DatetimeIndex(pd.to_datetime(list(days))))
    run = opt.run_options("NIFTY", bars, pd.Series(1, index=bars.index), vix, capital=500_000, lots=1, lot_size=65,
                          stop_pct=30, intraday=True, square_off="15:00", steps=NO_STEPS)
    first_day = [t for t in run["trades"] if t["entry_date"].startswith("2026-11-16")]
    assert len(first_day) == 1 and first_day[0]["exit"] == "stoploss"        # no buy again after it that day
    second = [t for t in run["trades"] if t["entry_date"].startswith("2026-11-17")]
    assert second and second[0]["entry_date"] == "2026-11-17 09:15"          # a new day buys again at the open
    assert all(t["exit"] in ("stoploss", "square-off", "open") for t in run["trades"])
    # The trade window in Settings, when enforced: no buy before it opens.
    run = opt.run_options("NIFTY", bars, pd.Series(1, index=bars.index), vix, capital=500_000, lots=1, lot_size=65,
                          stop_pct=30, intraday=True, square_off="15:00", steps=NO_STEPS, entry_window=("09:30", "14:30"))
    assert [t["entry_date"] for t in run["trades"]] == ["2026-11-16 09:30", "2026-11-17 09:30"]


def test_your_own_strategy_from_the_strategies_page_buys_index_options_intraday(tmp_path):
    from desk.custom import CustomStrategies

    bars = _five_minute()
    clock, engine, _, _ = _engine(tmp_path, "2026-11-18 11:02", bars)
    engine.library = CustomStrategies(tmp_path / "custom.json")
    # "Time of day" makes it an intraday-only strategy: refused on day candles, taken on 5-minute ones.
    key = engine.library.save({"name": "Morning up", "entry": {"match": "all", "rules": [
        {"left": {"indicator": "close"}, "op": ">", "right": {"value": 24850}},
        {"left": {"indicator": "time"}, "op": ">", "right": {"value": 930}}]},
        "exit": {"match": "all", "rules": [{"left": {"indicator": "close"}, "op": "<", "right": {"value": 24820}}]}})["key"]
    mine = {**CALLS_5M, "strategy": key, "strategy_params": {}}
    with pytest.raises(AlgoError, match="needs intraday candles"):
        engine.create({**mine, "name": "Mine daily", "interval": "1d"})
    algo = engine.create(mine)
    assert algo["strategy_name"] == "Morning up"
    engine.set_enabled(algo["id"], True)
    engine.evaluate(algo["id"])
    engine.get(algo["id"])["ready"] = True
    assert engine.evaluate(algo["id"])["algo"]["book"]["legs"][0]["type"] == "CE"
