"""Exit rules (stoploss and target in % or points, step trailing, stoploss to cost, re-entry) and time conditions."""

import numpy as np
import pandas as pd
import pytest

from desk import custom, optstrat
from desk.backtest import BacktestParams, run_strategy
from desk.risk import ExitRules, from_payload


def frame(prices, highs=None, lows=None, start="2024-01-01"):
    index = pd.bdate_range(start, periods=len(prices))
    p = pd.Series(prices, index=index, dtype=float)
    return pd.DataFrame({"Open": p, "High": pd.Series(highs, index=index, dtype=float) if highs else p,
                         "Low": pd.Series(lows, index=index, dtype=float) if lows else p, "Close": p, "Volume": 1.0})


def test_levels_trail_in_steps_and_move_to_cost():
    rules = ExitRules(unit="points", stop_loss=10, trail_every=10, trail_by=5).validate()
    assert rules.levels(100, 100) == (90, "stoploss", None)
    assert rules.levels(100, 112) == (95, "trailing stoploss", None)
    assert rules.levels(100, 131) == (105, "trailing stoploss", None)
    cost = ExitRules(unit="pct", stop_loss=50, cost_after=30).validate()
    assert cost.levels(100, 75, -1) == (150, "stoploss", None)              # a sold leg: rises are losses
    assert cost.levels(100, 70, -1) == (100, "stoploss at cost", None)
    assert ExitRules(unit="pct", target=20).levels(100, 100, -1)[2] == 80


@pytest.mark.parametrize("raw, fragment", [
    ({"unit": "pts"}, "% or points"), ({"stop_loss": -1}, "Stoploss"), ({"trail_every": 10}, "both boxes"),
    ({"trail_every": 10, "trail_by": 5}, "stoploss to start"), ({"reentry": 2}, "set a stoploss"),
    ({"stop_loss": 5, "reentry": 6}, "0 to 5"), ({"stop_loss": 5, "trail_every": 5, "trail_by": 8}, "further")])
def test_bad_exit_rules_are_refused_in_plain_words(raw, fragment):
    with pytest.raises(ValueError, match=fragment):
        from_payload(raw)


def test_points_stoploss_and_step_trail_in_the_stock_backtest():
    prices = [100.0] * 5 + [100, 105, 112, 121, 125, 119, 110] + [110.0] * 20
    lows = list(prices)
    rules = ExitRules(unit="points", stop_loss=10, trail_every=10, trail_by=5)
    result = run_strategy(frame(prices, lows=lows), "buy_hold", None,
                          BacktestParams(capital=100_000, slippage_bps=0, exits=rules))
    trade = result["trades"][0]
    # The high of 125 is 25 above the 100 entry: two steps of 10, so the stoploss is 90 + 2 × 5 = 100. 110 holds.
    assert trade["exit"] == "open"
    lows[11] = 99.0
    result = run_strategy(frame(prices, lows=lows), "buy_hold", None,
                          BacktestParams(capital=100_000, slippage_bps=0, exits=rules))
    assert result["trades"][0]["exit"] == "trailing stoploss" and result["trades"][0]["exit_price"] == 100.0


def test_stoploss_to_cost_exits_at_the_entry_price():
    prices = [100.0] * 30
    highs, lows = list(prices), list(prices)
    highs[3] = 106.0
    lows[5] = 99.0
    rules = ExitRules(unit="pct", stop_loss=5, cost_after=5)
    result = run_strategy(frame(prices, highs, lows), "buy_hold", None,
                          BacktestParams(capital=100_000, slippage_bps=0, exits=rules))
    assert result["trades"][0]["exit"] == "stoploss at cost" and result["trades"][0]["exit_price"] == 100.0


def test_reentry_waits_for_the_price_to_come_back_and_counts():
    prices = [100.0] * 4 + [100, 95, 96, 98, 101, 103] + [103.0] * 30
    highs, lows = list(prices), list(prices)
    lows[5] = 94.0                                                          # stoploss 5% = 95
    rules = ExitRules(unit="pct", stop_loss=5, reentry=1)
    result = run_strategy(frame(prices, highs, lows), "buy_hold", None,
                          BacktestParams(capital=100_000, slippage_bps=0, exits=rules))
    first, second = result["trades"][:2]
    # Out at 95; back in when a candle reaches 100 again: this one opens at 101, above it, so the fill is the open.
    assert first["exit"] == "stoploss" and first["exit_price"] == 95.0
    assert second["entry_price"] == 101.0 and second["entry_date"] == "2024-01-11"
    assert result["metrics"]["reentries"] == 1
    none = run_strategy(frame(prices, highs, lows), "buy_hold", None,
                        BacktestParams(capital=100_000, slippage_bps=0, exits=ExitRules(unit="pct", stop_loss=5)))
    assert len(none["trades"]) == 1                                         # no re-entry: buy and hold never resets


def test_reentry_straight_away_buys_at_the_next_open():
    prices = [100.0] * 4 + [100, 92, 86, 80] + [80.0] * 30     # gaps down: out at 92, in at 86, out at 80, in at 80
    rules = ExitRules(unit="pct", stop_loss=5, reentry=2, reentry_mode="at_once")
    result = run_strategy(frame(prices), "buy_hold", None, BacktestParams(capital=100_000, slippage_bps=0, exits=rules))
    trades = result["trades"]
    assert [t["entry_price"] for t in trades] == [100.0, 86.0, 80.0]          # each at the next open
    assert [t["exit"] for t in trades] == ["stoploss", "stoploss", "open"] and result["metrics"]["reentries"] == 2


def intraday(days=3):
    stamps = []
    for d in pd.bdate_range("2026-11-16", periods=days):
        stamps += list(pd.date_range(d + pd.Timedelta(hours=9, minutes=15), d + pd.Timedelta(hours=15, minutes=25), freq="5min"))
    index = pd.DatetimeIndex(stamps)
    p = pd.Series(np.linspace(100, 110, len(index)), index=index)
    return pd.DataFrame({"Open": p, "High": p + 0.1, "Low": p - 0.1, "Close": p, "Volume": 1.0})


def test_time_and_weekday_rules():
    spec = {"entry": {"match": "all", "rules": [
                {"left": {"indicator": "time"}, "op": ">=", "right": {"value": 1000}},
                {"left": {"indicator": "weekday"}, "op": "is_one_of", "right": {"days": [2]}}]},
            "exit": {"match": "any", "rules": [{"left": {"indicator": "time"}, "op": ">=", "right": {"value": 1430}}]}}
    strategy = custom.inline(spec)
    assert strategy.intraday_only and "Time of day is at or after 10:00" in strategy.description
    assert "Day of week is one of Tue" in strategy.description
    bars = intraday()
    result = run_strategy(bars, strategy, None, BacktestParams(intraday=True, trade_start="09:15", trade_end="15:00",
                                                               square_off="15:15", slippage_bps=0))
    assert len(result["trades"]) == 1
    trade = result["trades"][0]
    assert trade["entry_date"] == "2026-11-17 10:05" and trade["exit_date"] == "2026-11-17 14:35"


@pytest.mark.parametrize("rule, fragment", [
    ({"left": {"indicator": "time"}, "op": "crosses_above", "right": {"value": 920}}, "after or before"),
    ({"left": {"indicator": "time"}, "op": ">", "right": {"value": 975}}, "time like 09:20"),
    ({"left": {"indicator": "weekday"}, "op": ">", "right": {"value": 2}}, "is one of"),
    ({"left": {"indicator": "weekday"}, "op": "is_one_of", "right": {"days": []}}, "at least one day"),
    ({"left": {"indicator": "rsi", "params": {"period": 14}}, "op": "is_one_of", "right": {"days": [1]}}, "Day of week"),
    ({"left": {"indicator": "close"}, "op": ">", "right": {"indicator": "time"}}, "left side")])
def test_bad_time_rules_are_refused(rule, fragment):
    spec = {"name": "x", "entry": {"match": "all", "rules": [rule]},
            "exit": {"match": "any", "rules": [{"left": {"indicator": "close"}, "op": "<", "right": {"value": 1}}]}}
    with pytest.raises(ValueError, match=fragment):
        custom.validate(spec)


def test_a_time_rule_on_day_candles_is_refused():
    spec = {"entry": {"match": "all", "rules": [{"left": {"indicator": "time"}, "op": ">", "right": {"value": 930}}]},
            "exit": {"match": "any", "rules": [{"left": {"indicator": "close"}, "op": "<", "right": {"value": 1}}]}}
    with pytest.raises(ValueError, match="intraday candles"):
        run_strategy(frame([100.0] * 40), custom.inline(spec), None, BacktestParams())


def straddle_bars(days=6):
    stamps = []
    for d in pd.bdate_range("2026-01-05", periods=days):
        stamps += list(pd.date_range(d + pd.Timedelta(hours=9, minutes=15), d + pd.Timedelta(hours=15, minutes=25), freq="5min"))
    index = pd.DatetimeIndex(stamps)
    rng = np.random.default_rng(1)
    c = 24000 + np.cumsum(rng.normal(0, 15, len(index)))
    return pd.DataFrame({"Open": c, "High": c + 10, "Low": c - 10, "Close": c, "Volume": 1.0}, index=index)


VIX = pd.Series(14.0, index=pd.date_range("2025-12-01", "2026-03-01"))


def test_option_legs_reenter_after_a_stoploss():
    bars = straddle_bars()
    base = {"structure": "short_straddle", "mode": "intraday", "lot_size": 75, "expiry_kind": "weekly"}
    plain = optstrat.run_structure("NIFTY", bars, VIX, **base, leg_exits=ExitRules(unit="pct", stop_loss=30))
    again = optstrat.run_structure("NIFTY", bars, VIX, **base,
                                   leg_exits=ExitRules(unit="pct", stop_loss=30, reentry=2))
    assert plain["metrics"]["reentries"] == 0 and again["metrics"]["reentries"] > 0
    assert all(t["reentries"] <= 4 for t in again["trades"])                 # 2 legs × 2 re-entries
    legacy = optstrat.run_structure("NIFTY", bars, VIX, **base, leg_sl_pct=30)
    assert legacy["metrics"]["legs_stopped"] == plain["metrics"]["legs_stopped"]


def test_a_bought_hedge_is_never_closed_alone():
    result = optstrat.run_structure("NIFTY", straddle_bars(), VIX, structure="iron_condor", mode="intraday", lot_size=75,
                                    expiry_kind="weekly", leg_exits=ExitRules(unit="pct", stop_loss=40), leg_scope="all")
    assert any(t["exit"] == "stoploss on a bought leg" for t in result["trades"])
