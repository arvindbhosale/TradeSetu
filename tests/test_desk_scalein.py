"""Add to winners: more of a winning position bought at set profit levels, in every backtest."""

import numpy as np
import pandas as pd
import pytest

from desk import futures as fut, options as opt, optstrat, scalein, scaleout
from desk.backtest import BacktestParams, run_strategy
from desk.risk import ExitRules
from desk.scalein import Adds, from_payload, stop_price
from desk.scaleout import Steps
from tests.test_desk import client  # noqa: F401 - the fixture
from tests.test_desk_scaleout import days

pytestmark = pytest.mark.unit

VIX = pd.Series(14.0, index=pd.date_range("2025-12-01", "2026-12-01"))


def stock(bars, adds=None, steps=None, **kw):
    params = BacktestParams(capital=100_000, slippage_bps=0, adds=adds, steps=steps, **kw)
    return run_strategy(bars, "buy_hold", None, params)


# -- the adds themselves -------------------------------------------------------------------------------------------

def test_payload_is_checked_in_plain_words():
    assert from_payload(None, "stock") is scalein.NONE and not from_payload({"adds": []}, "futures").active
    assert not from_payload({"adds": [{"at": 0, "size": 0}]}, "futures").active             # a row left empty
    a = from_payload({"unit": "rs", "adds": [{"at": 1500, "size": 1}, {"at": 3000, "size": 2}], "scope": "position"},
                     "structure", lots=2)
    assert a.levels == (1500, 3000) and a.lots() == [1, 2] and a.stop_after == "risk" and a.scope == "position"
    condor = optstrat.legs_for("iron_condor", 2, 4)
    sold_only = optstrat.legs_for("short_straddle", 0, 4)
    for raw, kind, lots, legs, words in [
        ({"adds": [{"at": 2, "size": 1}, {"at": 1, "size": 1}]}, "futures", 1, None, "higher than the one before"),
        ({"adds": [{"at": 2, "size": 1.5}]}, "futures", 1, None, "whole number of lots"),
        ({"adds": [{"at": 2, "size": 30}, {"at": 3, "size": 30}]}, "futures", 1, None, "more than 50 lots"),
        ({"adds": [{"at": 2, "size": 200}, {"at": 3, "size": 50}]}, "stock", None, None, "between 0 and 100%"),
        ({"adds": [{"at": 1, "size": 100}, {"at": 2, "size": 100}, {"at": 3, "size": 100}, {"at": 4, "size": 10}]},
         "stock", None, None, "at most 300%"),
        ({"unit": "rs", "adds": [{"at": 2, "size": 50}]}, "stock", None, None, "measure in % or points"),
        ({"adds": [{"at": 2, "size": 1}], "stop_after": "none"}, "futures", 1, None, "average entry"),
        ({"adds": [{"at": 2, "size": 1}], "scope": "leg"}, "futures", 1, None, "for option strategies"),
        ({"adds": [{"at": 2, "size": 1}], "scope": "leg"}, "structure", 1, sold_only, "only a bought option leg"),
        ({"adds": [{"at": n, "size": 1} for n in range(1, 8)]}, "futures", 1, None, "at most 5 adds"),
    ]:
        with pytest.raises(ValueError, match=words):
            from_payload(raw, kind, lots, legs)
    assert from_payload({"adds": [{"at": 20, "size": 1}], "scope": "leg"}, "structure", 1, condor).scope == "leg"


def test_stoploss_after_an_add_keeps_the_planned_loss():
    # 100 shares at 100 with a stoploss at 95 risk ₹500. 50 more at 110: average 103.33. At 99.99 the 150 shares
    # lose 150 × 3.33 = ₹500, the same as planned.
    s = stop_price(1, 103.333333, 150, 500, "risk")
    assert s == pytest.approx(100.0, abs=0.01) and 150 * (103.333333 - s) == pytest.approx(500)
    assert stop_price(1, 103.33, 150, 500, "cost") == 103.33
    assert stop_price(1, 103.33, 150, 0, "risk") == 103.33               # no stoploss planned: breakeven
    assert stop_price(-1, 20000, 150, 15000, "risk") == pytest.approx(20100)   # a short: above the average
    assert scalein.floor_after(4000, "risk") == -4000 and scalein.floor_after(0, "risk") == 0
    assert Adds(levels=(10,), sizes=(50,)).first_budget(155_000, 100) == pytest.approx(100_000)


# -- stocks ----------------------------------------------------------------------------------------------------------

def test_stock_buys_more_at_each_level_and_moves_the_stoploss():
    closes = [100, 100, 102, 104, 106, 108, 110, 112] + [112] * 30
    r = stock(days(closes), Adds(unit="pct", levels=(5, 10), sizes=(50, 50)), exits=ExitRules(unit="pct", stop_loss=5))
    t = r["trades"][0]
    # The first buy leaves room for both adds at their prices: 100,000 / (1 + 0.5 × 1.05 + 0.5 × 1.10).
    first, half = 481, 240
    assert [a["quantity"] for a in t["adds"]] == [half, half]
    assert [a["price"] for a in t["adds"]] == [pytest.approx(105), pytest.approx(110)]
    assert t["quantity"] == first + 2 * half and t["entry_price"] == pytest.approx(100)
    assert t["average_price"] == pytest.approx((first * 100 + half * 105 + half * 110) / (first + 2 * half), abs=0.01)
    # The stoploss of all the shares keeps the planned loss (499 × ₹5) and only ever rises.
    planned = first * 5
    stop = t["adds"][-1]["stop"]
    assert t["quantity"] * (t["average_price"] - stop) == pytest.approx(planned, abs=15)
    assert t["adds"][0]["stop"] < stop < 105 and r["metrics"]["adds_made"] == 2 and r["metrics"]["adds_skipped"] == 0
    assert r["params"]["adds"]["adds"] == [{"at": 5, "size": 50}, {"at": 10, "size": 50}]


def test_stock_reversal_after_an_add_stops_out_near_the_planned_loss():
    closes = [100, 100, 103, 106, 101, 96, 90] + [90] * 30
    r = stock(days(closes), Adds(unit="pct", levels=(5,), sizes=(100,)), exits=ExitRules(unit="pct", stop_loss=5))
    t = r["trades"][0]
    assert t["exit"] == "stoploss moved after adding" and len(t["adds"]) == 1
    # Without the add the first buy would have lost ₹5 a share; with it the whole trade loses about that, not more.
    first = t["quantity"] - t["adds"][0]["quantity"]
    assert t["pnl"] + r["metrics"]["total_charges"] == pytest.approx(-first * 5, abs=1)
    cost = stock(days(closes), Adds(unit="pct", levels=(5,), sizes=(100,), stop_after="cost"),
                 exits=ExitRules(unit="pct", stop_loss=5))["trades"][0]
    assert cost["exit"] == "stoploss moved after adding" and cost["exit_price"] == pytest.approx(cost["average_price"])


def test_stock_add_is_skipped_when_the_cash_is_short_and_stops_after_a_profit_step():
    closes = [100, 100, 103, 106, 109, 112, 115] + [115] * 30
    bars = days(closes)
    # All the money in the first buy: nothing is left for the add.
    r = stock(bars, Adds(unit="pct", levels=(5,), sizes=(50,)))
    assert r["metrics"]["adds_skipped"] == 0 and r["trades"][0]["adds"][0]["quantity"] > 0
    full = run_strategy(bars, "buy_hold", None, BacktestParams(capital=100_000, slippage_bps=0, allocation_pct=50,
                                                                adds=Adds(unit="pct", levels=(5,), sizes=(50,))))
    assert full["trades"][0]["adds"][0]["quantity"] > 0                  # the other half of the money pays for it
    # A profit step at 4% comes first: from then on no add.
    stepped = stock(bars, Adds(unit="pct", levels=(5, 8), sizes=(50, 50)), Steps(unit="pct", levels=(4,), sizes=(50,)))
    t = stepped["trades"][0]
    assert t["partials"] and "adds" not in t and stepped["metrics"]["adds_made"] == 0


def test_stock_without_adds_is_unchanged():
    closes = [100, 100, 103, 106, 109, 112] + [112] * 30
    bars = days(closes)
    plain = stock(bars)
    assert "adds" not in plain["trades"][0] and plain["params"]["adds"] is None
    assert stock(bars, scalein.NONE)["metrics"] == plain["metrics"]


# -- futures, bought options, option strategies --------------------------------------------------------------------

def test_futures_add_lots_and_count_them_in_the_trade():
    closes = [20000, 20000, 20100, 20220, 20450, 20600] + [20600] * 30
    bars = days(closes)
    signal = pd.Series(1, index=bars.index)
    r = fut.run_futures(bars, signal, underlying="NIFTY", lot_size=75, lots=2, capital=5_000_000, continuous=False,
                        slippage_pct=0, stop_pct=1, adds=Adds(unit="pct", levels=(1, 2), sizes=(1, 1)))
    t = r["trades"][0]
    assert t["lots"] == 4 and t["quantity"] == 300 and [a["lots"] for a in t["adds"]] == [1, 1]
    assert [a["price"] for a in t["adds"]] == [pytest.approx(20200), pytest.approx(20400)]
    assert t["entry_price"] == pytest.approx(20000) and t["average_price"] == pytest.approx(20150)
    gross = 75 * (2 * 600 + 400 + 200)
    assert t["pnl"] == pytest.approx(gross - r["metrics"]["total_charges"], abs=0.05)
    # Margin short: the add is skipped and the trade goes on with its first lots.
    poor = fut.run_futures(bars, signal, underlying="NIFTY", lot_size=75, lots=2, capital=400_000, continuous=False,
                           slippage_pct=0, adds=Adds(unit="pct", levels=(1,), sizes=(5,)))
    assert poor["metrics"]["adds_skipped"] == 1 and poor["trades"][0]["lots"] == 2
    assert "skipped: margin short" in poor["trades"][0]["adds"][0]["why"]


def test_short_futures_add_as_the_price_falls_and_stop_above_the_average():
    closes = [20000, 20000, 19900, 19780, 19700, 19950, 20200] + [20200] * 30
    bars = days(closes)
    signal = pd.Series(0, index=bars.index)
    signal.iloc[0] = 1
    r = fut.run_futures(bars, signal, underlying="NIFTY", lot_size=75, lots=1, capital=5_000_000, continuous=False,
                        slippage_pct=0, direction="short", stop_pct=1,
                        adds=Adds(unit="points", levels=(200,), sizes=(1,)))
    t = r["trades"][0]
    assert t["side"] == "SHORT" and t["adds"][0]["price"] == pytest.approx(19800)
    assert t["exit"] == "stoploss moved after adding"
    # Planned loss: 75 × 200 points. Two lots at an average of 19,900 stop at 20,000: the same ₹15,000.
    assert t["adds"][0]["stop"] == pytest.approx(20000) and t["exit_price"] == pytest.approx(20000)


def test_bought_options_add_lots_on_the_closing_premium():
    rng = np.random.default_rng(4)
    closes = 24000 * np.exp(np.cumsum(np.r_[0, rng.normal(0.004, 0.006, 80)]))
    bars = days(closes)
    positions = pd.Series(1, index=bars.index)
    adds = Adds(unit="pct", levels=(20, 40), sizes=(1, 1))
    r = opt.run_options("NIFTY", bars, positions, VIX, capital=1_000_000, lots=1, lot_size=65, adds=adds)
    made = [a for t in r["trades"] for a in t.get("adds", []) if a["lots"]]
    assert made and r["metrics"]["adds_made"] == len(made)
    assert all(t["lots"] == 1 + len([a for a in t.get("adds", []) if a["lots"]]) for t in r["trades"])
    plain = opt.run_options("NIFTY", bars, positions, VIX, capital=1_000_000, lots=1, lot_size=65)
    assert plain["metrics"]["final_value"] != r["metrics"]["final_value"]
    assert all("adds" not in t for t in plain["trades"])


def test_option_strategy_adds_whole_lots_of_every_leg_bought_legs_first(monkeypatch):
    bars = days(np.full(60, 24000.0))                           # a still market: sold premium decays, profit grows
    sent = []
    real = optstrat.option_charges

    def spy(side, quantity, price, exchange):
        sent.append((side, quantity))
        return real(side, quantity, price, exchange)

    monkeypatch.setattr(optstrat, "option_charges", spy)
    adds = Adds(unit="pct", levels=(15, 30), sizes=(1, 1))
    r = optstrat.run_structure("NIFTY", bars, VIX, structure="iron_condor", mode="positional", lot_size=65, lots=2,
                               capital=5_000_000, weekdays=(0,), min_days=3, slippage_pct=0, adds=adds)
    added = [a for t in r["trades"] for a in t.get("adds", []) if a["lots"]]
    assert added and r["metrics"]["adds_made"] == len(added)
    assert all(t["lots"] == 2 + sum(a["lots"] for a in t.get("adds", [])) for t in r["trades"])
    # Each add is four one-lot orders: the two hedges bought, then the two sold legs; never a sold leg alone.
    one_lot = [c for c in sent if c[1] == 65]
    assert len(one_lot) >= 4 * len(added)
    assert r["params"]["adds"]["scope"] == "position"


def test_option_strategy_adds_to_a_winning_bought_leg_alone():
    closes = np.r_[np.full(3, 24000.0), np.linspace(24000, 25200, 20), np.full(20, 25200.0)]
    bars = days(closes)
    adds = Adds(unit="pct", levels=(30,), sizes=(2,), scope="leg")
    r = optstrat.run_structure("NIFTY", bars, VIX, structure="custom", mode="positional", lot_size=65, lots=1,
                               capital=5_000_000, weekdays=(0, 1, 2, 3, 4), min_days=20, slippage_pct=0, adds=adds,
                               legs=[{"side": "BUY", "type": "CE", "strikes": 0, "lots": 1},
                                     {"side": "BUY", "type": "PE", "strikes": 0, "lots": 1}])
    leg_adds = [a for t in r["trades"] for a in t.get("adds", []) if a["lots"]]
    assert leg_adds and all("of the bought" in a["why"] and "CE" in a["why"] for a in leg_adds)
    with pytest.raises(ValueError, match="only a bought option leg"):
        optstrat.run_structure("NIFTY", bars, VIX, structure="short_straddle", mode="positional", lot_size=65, k=0,
                               adds=adds)


def test_every_backtest_with_adds_is_compared_with_none():
    closes = [100, 100, 103, 106, 101, 96, 90] + [90] * 30
    bars = days(closes)
    adds = Adds(unit="pct", levels=(5,), sizes=(100,))
    out = scalein.run_all(lambda s, a: stock(bars, a if a.active else None, s if s.active else None),
                          scaleout.NONE, adds)
    c = out["adds_compare"]
    assert c["adds_made"] == 1 and c["better"] in ("adds", "without") and "steps_compare" not in out
    assert c["without_adds"]["final_value"] != c["with_adds"]["final_value"]
    both = scalein.run_all(lambda s, a: stock(bars, a if a.active else None, s if s.active else None),
                           Steps(unit="pct", levels=(2,), sizes=(50,)), adds)
    assert "steps_compare" in both and "adds_compare" in both


# -- the API -----------------------------------------------------------------------------------------------------------

def test_api_backtests_take_adds_and_compare_them(client):  # noqa: F811 - the fixture
    adds = {"unit": "pct", "adds": [{"at": 3, "size": 50}], "stop_after": "risk"}
    body = {"symbol": "RELIANCE", "start": "2022-06-01", "end": "2024-03-01", "strategy": "ema_cross",
            "exits": {"unit": "pct", "stop_loss": 5, "target": 12}, "adds": adds}
    r = client.post("/api/backtest/strategy", json=body).json()
    assert r["params"]["adds"]["adds"] == [{"at": 3, "size": 50}]
    c = r["adds_compare"]
    assert c["adds_made"] == r["metrics"]["adds_made"] > 0 and c["better"] in ("adds", "without", "same")
    plain = client.post("/api/backtest/strategy", json={**body, "adds": None}).json()
    assert "adds_compare" not in plain and plain["metrics"]["final_value"] == c["without_adds"]["final_value"]
    bad = client.post("/api/backtest/strategy", json={**body, "adds": {"adds": [{"at": 4, "size": 120}]}})
    assert bad.status_code == 400 and "between 0 and 100%" in bad.json()["error"]
    futures = client.post("/api/backtest/futures", json={"underlying": "NIFTY", "start": "2022-06-01",
                                                         "end": "2024-03-01", "strategy": "ema_cross", "lots": 2,
                                                         "adds": {"unit": "pct", "adds": [{"at": 1, "size": 1}]}})
    assert futures.status_code == 200 and "adds_compare" in futures.json()


def test_the_journal_lists_each_buy_of_a_trade_with_adds(tmp_path):
    from desk import results
    from desk.pnl import Ledger

    ledger = Ledger(tmp_path / "fills.jsonl")
    for side, qty, price, when in (("BUY", 10, 100, "2026-11-16 09:30:00"), ("BUY", 5, 110, "2026-11-16 10:05:00"),
                                   ("SELL", 15, 120, "2026-11-16 14:00:00"), ("BUY", 4, 90, "2026-11-17 09:30:00"),
                                   ("SELL", 4, 95, "2026-11-17 10:00:00")):
        ledger.add("paper", "algo:One", "NSE", "INFY", side, qty, price, "CNC", charges=0, time_ist=when)
    trades = results.positions(ledger.closed_trades("paper"), [], {})
    added, plain = sorted(trades, key=lambda t: t["opened_at"])
    assert [(e["time"], e["quantity"], e["price"]) for e in added["entries"]] == [
        ("2026-11-16 09:30:00", 10, 100), ("2026-11-16 10:05:00", 5, 110)]
    assert added["entry"] == pytest.approx(103.33, abs=0.01) and len(plain["entries"]) == 1
