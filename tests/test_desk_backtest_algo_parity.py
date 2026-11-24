"""An algo made from a backtest exits the way the backtest tested: rupee and % stops and targets on both sides, and
the whole position's stop watched inside the candle as the live algo watches the price."""

import numpy as np
import pandas as pd
import pytest

from desk import futures as fut, options as opt, optstrat
from tests.test_desk_scaleout import days

pytestmark = pytest.mark.unit

VIX = pd.Series(14.0, index=pd.date_range("2025-12-01", "2026-12-01"))


# -- index futures backtest: maximum loss and target in ₹ ------------------------------------------------------------

def test_futures_maximum_loss_in_rupees_is_a_price_level_inside_the_candle():
    closes = [20000, 20000, 19950, 19900, 20000] + [20000] * 30
    lows = [20000, 20000, 19950, 19700, 19950] + [20000] * 30
    bars = days(closes, lows=lows)
    signal = pd.Series(1, index=bars.index)
    r = fut.run_futures(bars, signal, underlying="NIFTY", lot_size=75, lots=1, capital=5_000_000, continuous=False,
                        slippage_pct=0, stop_rs=15_000)
    t = r["trades"][0]
    assert t["exit"] == "maximum loss in ₹"
    # Entry charges count toward the loss, as the algo's maximum loss counts them: the stop sits a little above 19,800.
    assert 19_800 < t["exit_price"] < 19_810
    assert -16_000 < t["pnl"] < -15_000                                    # the exit's own charges on top
    assert r["params"]["stop_rs"] == 15_000


def test_futures_target_in_rupees_and_the_tighter_of_the_two_stops():
    closes = [20000, 20000, 20100, 20300, 20300] + [20300] * 30
    bars = days(closes)
    signal = pd.Series(1, index=bars.index)
    r = fut.run_futures(bars, signal, underlying="NIFTY", lot_size=75, lots=2, capital=5_000_000, continuous=False,
                        slippage_pct=0, target_rs=30_000)
    t = r["trades"][0]
    assert t["exit"] == "target in ₹" and 20_200 < t["exit_price"] < 20_210
    # A short with a 2% stop (20,400) and a ₹7,500 maximum loss (about 20,100): the rupee one is reached first.
    down = days([20000, 20000, 20050, 20150, 20500] + [20500] * 30)
    short = pd.Series(0, index=down.index)
    short.iloc[0] = 1
    r = fut.run_futures(down, short, underlying="NIFTY", lot_size=75, lots=1, capital=5_000_000, continuous=False,
                        slippage_pct=0, direction="short", stop_pct=2, stop_rs=7_500)
    assert r["trades"][0]["exit"] == "maximum loss in ₹" and r["trades"][0]["exit_price"] < 20_101
    with pytest.raises(ValueError, match="Maximum loss"):
        fut.run_futures(bars, signal, underlying="NIFTY", lot_size=75, stop_rs=-1)


# -- options buying backtest: inside the candle, and in ₹ -----------------------------------------------------------

def test_bought_option_stoploss_fills_at_its_level_when_the_candle_reaches_it():
    closes = [24000] * 5 + [24000, 24000, 24000] + [24000] * 30
    lows = list(closes)
    lows[6] = 23_300                                     # a deep dip that recovers by the close
    bars = days(closes, lows=lows)
    positions = pd.Series(1, index=bars.index)
    r = opt.run_options("NIFTY", bars, positions, VIX, capital=1_000_000, lots=1, lot_size=65, stop_pct=40,
                        target_pct=0, slippage_pct=0, min_days=25)
    first = r["trades"][0]
    assert first["exit"] == "stoploss" and first["exit_date"].startswith(str(bars.index[6].date()))
    assert first["exit_price"] == pytest.approx(first["entry_price"] * 0.6, rel=0.02)


def test_bought_option_maximum_loss_and_target_in_rupees():
    closes = [24000, 24000] + list(np.linspace(24000, 23400, 10)) + [23400] * 30
    bars = days(closes)
    positions = pd.Series(1, index=bars.index)
    r = opt.run_options("NIFTY", bars, positions, VIX, capital=1_000_000, lots=1, lot_size=65, stop_pct=0,
                        target_pct=0, slippage_pct=0, stop_rs=3_000, min_days=25)
    t = r["trades"][0]
    # At the level, or a little past it when the day opens beyond it (a night's time decay, a gap).
    assert t["exit"] == "maximum loss in ₹" and -3_300 < t["pnl"] <= -2_990
    up = days([24000] * 5 + list(np.linspace(24000, 24600, 10)) + [24600] * 30)
    r = opt.run_options("NIFTY", up, pd.Series(1, index=up.index), VIX, capital=1_000_000, lots=1, lot_size=65,
                        stop_pct=0, target_pct=0, slippage_pct=0, target_rs=4_000, min_days=25)
    t = r["trades"][0]
    assert t["exit"] == "target in ₹" and 3_900 < t["pnl"] < 4_400
    assert r["params"]["target_rs"] == 4_000


# -- option strategies: the whole position's stop inside the candle; no entry again on a stopped candle -------------

def test_option_strategy_rupee_stoploss_is_watched_inside_the_day_candle():
    closes = np.full(40, 24000.0)
    highs = closes.copy()
    highs[8] = 24_900                                    # a spike that the close gives back
    bars = days(closes, highs=highs)
    common = {"structure": "short_straddle", "mode": "positional", "lot_size": 65, "lots": 1, "k": 0,
              "capital": 5_000_000, "weekdays": (0, 1, 2, 3, 4), "min_days": 10, "slippage_pct": 0, "stop_rs": 20_000}
    r = optstrat.run_structure("NIFTY", bars, VIX, **common)
    hit = [t for t in r["trades"] if t["exit"] == "stoploss in ₹"]
    assert hit and hit[0]["exit_date"] == str(bars.index[8].date())
    # Closed at about the stop, not at the close's smaller loss and not at the spike's larger one.
    assert hit[0]["pnl"] == pytest.approx(-20_000, abs=1_500)
    assert 24_000 < hit[0]["exit_price"] < 24_900
    calm = optstrat.run_structure("NIFTY", days(closes), VIX, **common)
    assert not [t for t in calm["trades"] if t["exit"] == "stoploss in ₹"]


def test_option_strategy_percent_target_inside_the_candle_waits_for_pending_profit_steps():
    closes = np.r_[np.full(5, 24000.0), np.linspace(24000, 22800, 8), np.full(30, 22800.0)]
    bars = days(closes)
    r = optstrat.run_structure("NIFTY", bars, VIX, structure="bear_put_spread", mode="positional", lot_size=65,
                               lots=1, w=4, capital=5_000_000, weekdays=(0,), min_days=14, slippage_pct=0,
                               target_pct=60)
    assert any(t["exit"] == "target" for t in r["trades"])


def test_a_signal_entry_does_not_reopen_on_the_candle_its_stop_closed():
    closes = np.r_[np.linspace(23000, 24000, 30), np.full(30, 24000.0)]
    lows = closes.copy()
    lows[40] = 23_000                                    # the stop is hit on candle 40, the signal stays on
    bars = days(closes, lows=lows)
    signal = pd.Series(1, index=bars.index)
    r = optstrat.run_structure("NIFTY", bars, VIX, structure="bull_call_spread", mode="signal", lot_size=65, lots=1,
                               w=4, capital=5_000_000, min_days=5, slippage_pct=0, stop_rs=1_500, signal=signal)
    stopped = [t for t in r["trades"] if t["exit"] == "stoploss in ₹"]
    assert stopped
    day = stopped[0]["exit_date"]
    assert not [t for t in r["trades"] if t["entry_date"] == day], "re-entered on the candle that stopped it"
    after = [t for t in r["trades"] if t["entry_date"] > day]
    assert after and after[0]["entry_date"] == str(bars.index[bars.index.get_loc(pd.Timestamp(day)) + 1].date())
