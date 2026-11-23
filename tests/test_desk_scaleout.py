"""Book profit in steps: part of a position closed at each profit level, in every backtest and algo."""

import numpy as np
import pandas as pd
import pytest

from desk import futures as fut, options as opt, optstrat, scaleout
from desk.backtest import BacktestParams, run_strategy
from desk.scaleout import Steps, from_payload
from tests.test_desk import client  # noqa: F401 - the fixture

pytestmark = pytest.mark.unit


def days(closes, highs=None, lows=None, start="2026-01-05"):
    closes = np.asarray(closes, float)
    opens = np.r_[closes[0], closes[:-1]]
    highs = np.maximum(opens, closes) if highs is None else np.asarray(highs, float)
    lows = np.minimum(opens, closes) if lows is None else np.asarray(lows, float)
    return pd.DataFrame({"Open": opens, "High": highs, "Low": lows, "Close": closes, "Volume": 1e5},
                        index=pd.bdate_range(start, periods=len(closes)))


def stock(bars, steps, **kw):
    params = BacktestParams(capital=100_000, slippage_bps=0, steps=steps, **kw)
    return run_strategy(bars, "buy_hold", None, params)


# -- the steps themselves ------------------------------------------------------------------------------------------

def test_payload_is_checked_in_plain_words():
    assert from_payload(None, "stock") is scaleout.NONE and not from_payload({"steps": []}, "futures").active
    assert not from_payload({"steps": [{"at": 0, "size": 0}]}, "futures").active        # a row left empty
    s = from_payload({"unit": "rs", "steps": [{"at": 1500, "size": 1}, {"at": 3000, "size": 2}], "stop_after": "cost"},
                     "structure", lots=4)
    assert s.levels == (1500, 3000) and s.lots() == [1, 2] and s.stop_after == "cost"
    for raw, kind, lots, words in [
        ({"steps": [{"at": 2, "size": 1}, {"at": 1, "size": 1}]}, "futures", 4, "higher than the one before"),
        ({"steps": [{"at": 2, "size": 1.5}]}, "futures", 4, "whole number of lots"),
        ({"steps": [{"at": 2, "size": 3}, {"at": 3, "size": 2}]}, "structure", 4, "close 5 lots but the position has 4"),
        ({"steps": [{"at": 2, "size": 60}, {"at": 3, "size": 50}]}, "stock", None, "more than the 100%"),
        ({"unit": "rs", "steps": [{"at": 2, "size": 50}]}, "stock", None, "measure in % or points"),
        ({"unit": "points", "steps": [{"at": 2, "size": 1}]}, "structure", 2, "measure in % or ₹ a lot"),
        ({"steps": [{"at": 2, "size": 1}], "stop_after": "later"}, "futures", 2, "stoploss stays"),
    ]:
        with pytest.raises(ValueError, match=words):
            from_payload(raw, kind, lots)


def test_shares_round_down_and_the_last_step_takes_what_is_left():
    s = Steps(levels=(1, 2, 3), sizes=(33.33, 33.33, 33.34))
    assert s.shares(10) == [3, 3, 4]
    assert Steps(levels=(1, 2), sizes=(25, 25)).shares(10) == [2, 2]        # half the shares stay for the exit rule
    assert Steps(levels=(1,), sizes=(10,)).shares(5) == [0]                 # too few shares for the step: skipped


def test_stoploss_moves_to_cost_then_to_the_previous_step():
    s = Steps(levels=(10, 20, 30), sizes=(1, 1, 1), stop_after="previous")
    assert [s.lock(n) for n in range(4)] == [None, 0.0, 10, 20]
    assert Steps(levels=(10, 20), sizes=(1, 1), stop_after="cost").lock(2) == 0.0
    assert Steps(levels=(10,), sizes=(1,)).lock(1) is None


# -- stocks ----------------------------------------------------------------------------------------------------------

def test_stock_sells_half_at_each_level_and_reports_each_sale():
    closes = [100, 100, 102, 104, 106, 108, 110, 112, 114] + [114] * 30
    bars = days(closes)
    r = stock(bars, Steps(unit="pct", levels=(5, 10), sizes=(50, 50)))
    t = r["trades"][0]
    assert t["exit"] == "profit step 2" and not t["open"] and t["quantity"] == 998      # 1,000 less the charges
    first, = t["partials"]
    assert first["why"] == "profit step 1" and first["price"] == pytest.approx(105) and first["date"] == "2026-01-09"
    assert first["quantity"] * 2 == t["quantity"] and t["exit_price"] == pytest.approx(110)
    assert t["pnl"] == pytest.approx(5 * 499 + 10 * 499 - r["metrics"]["total_charges"], abs=0.02)
    assert r["metrics"]["steps_booked"] == 1 and r["metrics"]["trades"] == 1
    assert r["params"]["steps"]["steps"][0] == {"at": 5, "size": 50}


def test_stock_stoploss_moves_to_cost_after_the_first_step():
    closes = [100, 100, 103, 106, 103, 99, 97] + [97] * 30
    r = stock(days(closes), Steps(unit="pct", levels=(5, 20), sizes=(50, 50), stop_after="cost"))
    t = r["trades"][0]
    assert t["exit"] == "stoploss at cost after a profit step" and t["exit_price"] == pytest.approx(100)
    assert len(t["partials"]) == 1 and t["pnl"] > 0                     # the rest sold at cost, not at 97


def test_stock_without_steps_is_unchanged_and_a_target_below_a_step_comes_first():
    closes = [100, 100, 103, 106, 109, 112] + [112] * 30
    bars = days(closes)
    plain = stock(bars, None)
    assert "partials" not in plain["trades"][0] and plain["params"]["steps"] is None
    from desk.risk import ExitRules
    target_first = stock(bars, Steps(unit="pct", levels=(8,), sizes=(50,)), exits=ExitRules(unit="pct", target=5))
    t = target_first["trades"][0]
    assert t["exit"] == "target" and "partials" not in t


# -- futures, bought options, option strategies --------------------------------------------------------------------

def test_futures_close_lots_at_each_level_and_the_rest_at_the_previous_step():
    closes = [20000, 20000, 20100, 20220, 20450, 20300, 20150, 20100] + [20100] * 30
    bars = days(closes)
    signal = pd.Series(1, index=bars.index)
    signal.iloc[6:] = 0                                         # no new entry after the stop
    steps = Steps(unit="pct", levels=(1, 2), sizes=(1, 1), stop_after="previous")
    r = fut.run_futures(bars, signal, underlying="NIFTY", lot_size=75, lots=4, capital=5_000_000, continuous=False,
                        slippage_pct=0, steps=steps)
    t = r["trades"][0]
    assert [p["lots"] for p in t["partials"]] == [1, 1] and t["lots"] == 4
    assert t["partials"][0]["price"] == pytest.approx(20200) and t["partials"][1]["price"] == pytest.approx(20400)
    assert t["exit"] == "stoploss at profit step 1" and t["exit_price"] == pytest.approx(20200)
    gross = 75 * (200 + 400 + 2 * 200)
    assert t["pnl"] == pytest.approx(gross - r["metrics"]["total_charges"], abs=0.05)
    with pytest.raises(ValueError, match="close 5 lots but the position has 4"):
        fut.run_futures(bars, signal, underlying="NIFTY", lot_size=75, lots=4, continuous=False,
                        steps=Steps(unit="pct", levels=(1, 2), sizes=(2, 3)))


def test_short_futures_book_profit_as_the_price_falls():
    closes = [20000, 20000, 19900, 19780, 19500] + [19500] * 30
    bars = days(closes)
    signal = pd.Series(0, index=bars.index)
    signal.iloc[0] = 1                                          # one buy signal first: shorts start after it
    r = fut.run_futures(bars, signal, underlying="NIFTY", lot_size=75, lots=2, capital=5_000_000, continuous=False,
                        slippage_pct=0, direction="short", steps=Steps(unit="points", levels=(200,), sizes=(1,)))
    t = r["trades"][0]
    assert t["side"] == "SHORT" and t["partials"][0]["lots"] == 1 and t["partials"][0]["pnl"] > 0


def test_bought_options_book_lots_on_the_closing_premium():
    rng = np.random.default_rng(4)
    closes = 24000 * np.exp(np.cumsum(np.r_[0, rng.normal(0.004, 0.006, 80)]))
    bars = days(closes)
    vix = pd.Series(14.0, index=pd.date_range("2025-12-01", "2026-12-01"))
    positions = pd.Series(1, index=bars.index)
    steps = Steps(unit="pct", levels=(20, 40), sizes=(1, 1))
    r = opt.run_options("NIFTY", bars, positions, vix, capital=1_000_000, lots=3, lot_size=65, steps=steps)
    booked = [p for t in r["trades"] for p in t.get("partials", [])]
    assert booked and all(p["lots"] == 1 and p["pnl"] > 0 for p in booked)
    assert r["metrics"]["steps_booked"] == len(booked)
    single = opt.run_options("NIFTY", bars, positions, vix, capital=1_000_000, lots=3, lot_size=65)
    assert single["metrics"]["final_value"] != r["metrics"]["final_value"]


def test_option_strategy_closes_matched_lots_of_every_leg(monkeypatch):
    bars = days(np.full(60, 24000.0))                           # a still market: sold premium decays, profit grows
    vix = pd.Series(14.0, index=pd.date_range("2025-12-01", "2026-12-01"))
    closed = []
    real = optstrat.option_charges

    def spy(side, quantity, price, exchange):
        closed.append((side, quantity))
        return real(side, quantity, price, exchange)

    monkeypatch.setattr(optstrat, "option_charges", spy)
    steps = Steps(unit="pct", levels=(15, 30), sizes=(1, 1), stop_after="cost")
    r = optstrat.run_structure("NIFTY", bars, vix, structure="iron_condor", mode="positional", lot_size=65, lots=4,
                               capital=5_000_000, weekdays=(0,), min_days=3, slippage_pct=0, steps=steps)
    with_parts = [t for t in r["trades"] if t.get("partials")]
    assert with_parts and all(p["lots"] == 1 and p["quantity"] == 65 for t in with_parts for p in t["partials"])
    # Every partial close is four orders of one lot: both sold legs and both hedges, never a sold leg alone.
    one_lot = [c for c in closed if c[1] == 65]
    assert len(one_lot) == 4 * r["metrics"]["steps_booked"]
    assert sum(1 for c in one_lot if c[0] == "BUY") == sum(1 for c in one_lot if c[0] == "SELL")
    assert r["params"]["steps"]["unit"] == "pct"


def test_option_strategy_refuses_steps_beyond_its_lots_and_units_it_cannot_measure():
    bars = days(np.full(40, 24000.0))
    vix = pd.Series(14.0, index=pd.date_range("2025-12-01", "2026-12-01"))
    with pytest.raises(ValueError, match="close 3 lots but the position has 2"):
        optstrat.run_structure("NIFTY", bars, vix, structure="iron_condor", mode="positional", lot_size=65, lots=2,
                               steps=Steps(unit="rs", levels=(100, 200), sizes=(1, 2)))
    with pytest.raises(ValueError, match="₹ a lot or %"):
        optstrat.run_structure("NIFTY", bars, vix, structure="iron_condor", mode="positional", lot_size=65, lots=2,
                               steps=Steps(unit="points", levels=(100,), sizes=(1,)))


def test_every_backtest_with_steps_is_compared_with_one_exit():
    closes = [100, 100, 103, 106, 103, 99, 97] + [97] * 30
    bars = days(closes)
    steps = Steps(unit="pct", levels=(5,), sizes=(50,), stop_after="cost")
    out = scaleout.run_both(lambda s: stock(bars, s if s.active else None), steps)
    c = out["steps_compare"]
    assert c["better"] == "steps" and c["difference"] > 0 and c["steps_booked"] == 1
    assert c["single_exit"]["final_value"] < c["with_steps"]["final_value"]
    assert "steps_compare" not in scaleout.run_both(lambda s: stock(bars, None), scaleout.NONE)


# -- the API -----------------------------------------------------------------------------------------------------------

def test_api_backtests_take_steps_and_compare_them_with_one_exit(client):  # noqa: F811 - the fixture
    steps = {"unit": "pct", "steps": [{"at": 4, "size": 50}], "stop_after": "cost"}
    body = {"symbol": "RELIANCE", "start": "2022-06-01", "end": "2024-03-01", "strategy": "ema_cross",
            "exits": {"unit": "pct", "stop_loss": 5, "target": 12}, "steps": steps}
    r = client.post("/api/backtest/strategy", json=body).json()
    assert r["params"]["steps"]["steps"] == [{"at": 4, "size": 50}]
    c = r["steps_compare"]
    assert c["steps_booked"] == r["metrics"]["steps_booked"] > 0 and c["better"] in ("steps", "single", "same")
    assert c["single_exit"]["final_value"] != c["with_steps"]["final_value"]
    assert any(t.get("partials") for t in r["trades"])
    plain = client.post("/api/backtest/strategy", json={**body, "steps": None}).json()
    assert "steps_compare" not in plain and plain["metrics"]["final_value"] == c["single_exit"]["final_value"]
    bad = client.post("/api/backtest/strategy", json={**body, "steps": {"steps": [{"at": 4, "size": 120}]}})
    assert bad.status_code == 400 and "between 0 and 100% of the shares" in bad.json()["error"]
    fut = client.post("/api/backtest/futures", json={"underlying": "NIFTY", "start": "2022-06-01", "end": "2024-03-01",
                                                     "strategy": "ema_cross", "lots": 2,
                                                     "steps": {"unit": "pct", "steps": [{"at": 1, "size": 3}]}})
    assert fut.status_code == 400 and "close 3 lots but the position has 2" in fut.json()["error"]
