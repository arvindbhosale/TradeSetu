"""F&O algos book profit in steps: matched lots of every leg, sold legs bought back before their hedges."""

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("fastapi")
pytest.importorskip("mcp")

from desk.algo import AlgoError
from desk.fnoalgo import arm_phrase
from tests.test_desk_fno_algos import CONDOR, Clock, Market, StubKite, journal, setup

pytestmark = pytest.mark.unit
STEPS = {"unit": "rs", "steps": [{"at": 1000, "size": 1}, {"at": 2000, "size": 1}], "stop_after": "cost"}


def decay(prices, book, factor):
    """Every leg's premium at ``factor`` of its entry: below 1 the sold legs gain more than the hedges lose."""
    for g in book["legs"]:
        prices.override[g["instrument"]] = round(g["avg"] * factor, 2)


def test_steps_are_checked_against_the_lots(tmp_path):
    engine, _, _ = setup(tmp_path, Clock("2026-11-17 09:00"))
    with pytest.raises(AlgoError, match="close 3 lots but the position has 2"):
        engine.create({**CONDOR, "lots": 2, "steps": {"unit": "rs", "steps": [{"at": 500, "size": 1}, {"at": 900, "size": 2}]}})
    with pytest.raises(AlgoError, match="whole number of lots"):
        engine.create({**CONDOR, "lots": 2, "steps": {"unit": "rs", "steps": [{"at": 500, "size": 0.5}]}})
    with pytest.raises(AlgoError, match="% or ₹ a lot"):
        engine.create({**CONDOR, "lots": 2, "steps": {"unit": "points", "steps": [{"at": 5, "size": 1}]}})
    algo = engine.create({**CONDOR, "lots": 3, "steps": STEPS})
    assert algo["steps"]["steps"] == [{"at": 1000, "size": 1}, {"at": 2000, "size": 1}]
    assert "book profit in steps: 1 lot at ₹1,000 profit a lot" in algo["log"][-1]["message"]


def test_a_paper_condor_closes_one_lot_of_every_leg_at_each_step_and_the_rest_at_cost(tmp_path):
    clock = Clock("2026-11-17 09:35")
    engine, desk, prices = setup(tmp_path, clock)
    algo = engine.create({**CONDOR, "lots": 3, "steps": STEPS})
    engine.set_enabled(algo["id"], True)
    book = engine.evaluate(algo["id"])["algo"]["book"]
    assert book["lots_open"] == 3 and all(g["open_qty"] == 195 for g in book["legs"])
    opened = len(journal(desk))
    clock.set("2026-11-17 10:30")
    decay(prices, book, 0.8)                                    # about ₹1,300 a lot: the first step only
    out = engine.evaluate(algo["id"])
    book = out["algo"]["book"]
    assert book["lots_open"] == 2 and book["steps_done"] == 1
    assert all(g["open_qty"] == 130 for g in book["legs"])
    step = journal(desk)[opened:]
    assert [(r["side"], r["quantity"]) for r in step] == [("BUY", 65), ("BUY", 65), ("SELL", 65), ("SELL", 65)]
    assert [r["tradingsymbol"] for r in step[:2]] == [g["tradingsymbol"] for g in book["legs"] if g["action"] < 0]
    assert book["partials"][0]["lots"] == 1 and book["partials"][0]["time"] == "2026-11-17 10:30"
    for g in book["legs"]:
        leg_row = desk.paper.quantity(g["instrument"])
        assert leg_row == (130 if g["action"] > 0 else -130)            # never a sold leg without its wing
    # The profit falls back to the entry: the stoploss moved to cost closes the two lots left.
    clock.set("2026-11-17 11:00")
    decay(prices, book, 1.0)
    out = engine.evaluate(algo["id"])
    assert out["algo"]["book"] is None
    trade = out["algo"]["history"][-1]
    assert trade["reason"].startswith("stoploss at cost after a profit step")
    assert trade["partials"][0]["lots"] == 1


def test_the_last_step_closes_every_lot_left(tmp_path):
    clock = Clock("2026-11-17 09:35")
    engine, desk, prices = setup(tmp_path, clock)
    algo = engine.create({**CONDOR, "lots": 2, "steps": {**STEPS, "stop_after": "none"}})
    engine.set_enabled(algo["id"], True)
    book = engine.evaluate(algo["id"])["algo"]["book"]
    clock.set("2026-11-17 10:30")
    decay(prices, book, 0.8)
    assert engine.evaluate(algo["id"])["algo"]["book"]["lots_open"] == 1
    decay(prices, book, 0.6)                                    # about ₹2,600 a lot: past the last step
    out = engine.evaluate(algo["id"])
    assert out["algo"]["book"] is None and out["algo"]["history"][-1]["reason"].startswith("profit step 2")
    assert desk.pnl("paper")["open"] == []


def test_live_steps_buy_back_the_sold_lots_before_selling_their_hedges(tmp_path):
    clock = Clock("2026-11-17 09:35")
    kite = StubKite(Market())
    engine, desk, prices = setup(tmp_path, clock, kite=kite, live_enabled=True)
    kite.prices = prices
    algo = engine.create({**CONDOR, "lots": 2, "mode": "live", "max_loss": 9000, "steps": STEPS})
    engine.set_enabled(algo["id"], True, arm_phrase(algo["name"]))
    engine.evaluate(algo["id"])
    for p in kite.placed:
        kite.fill(p["order_id"])
    engine.evaluate(algo["id"])
    for p in kite.placed[2:]:
        kite.fill(p["order_id"])
    book = engine.evaluate(algo["id"])["algo"]["book"]
    assert book["phase"] == "open"
    before = len(kite.placed)
    clock.set("2026-11-17 10:30")
    decay(prices, book, 0.8)
    engine.evaluate(algo["id"])
    first = kite.placed[before:]
    assert [(p["transaction_type"], p["quantity"]) for p in first] == [("BUY", 65), ("BUY", 65)]
    for p in first:
        kite.fill(p["order_id"])
    engine.evaluate(algo["id"])
    hedges = kite.placed[before + 2:]
    assert [(p["transaction_type"], p["quantity"]) for p in hedges] == [("SELL", 65), ("SELL", 65)]
    for p in hedges:
        kite.fill(p["order_id"])
    out = engine.evaluate(algo["id"])
    book = out["algo"]["book"]
    assert book["phase"] == "open" and book["lots_open"] == 1 and all(g["open_qty"] == 65 for g in book["legs"])
    assert not any(g["closing"] or "close_to" in g for g in book["legs"])


def test_a_futures_algo_books_lots_on_the_price_move(tmp_path):
    clock = Clock("2026-11-17 09:35")
    engine, desk, prices = setup(tmp_path, clock, max_order_value=5_000_000.0)
    days = pd.bdate_range(end="2026-11-16", periods=120)
    closes = np.r_[np.linspace(26000, 24000, 80), np.linspace(24000, 25500, 40)]
    engine.fetch = lambda symbol, interval="1d": pd.DataFrame(
        {"Open": closes, "High": closes * 1.003, "Low": closes * 0.997, "Close": closes, "Volume": 1e6}, index=days)
    algo = engine.create({"name": "Nifty fut", "kind": "futures", "underlying": "NIFTY", "direction": "long",
                          "strategy": "sma_cross", "strategy_params": {"fast": 5, "slow": 20}, "lots": 2,
                          "mode": "paper", "entry_time": "09:35", "exit_time": "15:10",
                          "steps": {"unit": "points", "steps": [{"at": 100, "size": 1}]}})
    engine.set_enabled(algo["id"], True)
    engine.evaluate(algo["id"])
    engine.get(algo["id"])["ready"] = True
    book = engine.evaluate(algo["id"])["algo"]["book"]
    fut = book["legs"][0]
    assert fut["open_qty"] == 130
    prices.override[fut["instrument"]] = fut["avg"] + 120
    out = engine.evaluate(algo["id"])
    assert out["algo"]["book"]["lots_open"] == 1 and out["algo"]["book"]["legs"][0]["open_qty"] == 65
    assert journal(desk)[-1]["side"] == "SELL" and journal(desk)[-1]["quantity"] == 65


def test_steps_can_change_while_legs_are_open(tmp_path):
    clock = Clock("2026-11-17 09:35")
    engine, _, _ = setup(tmp_path, clock)
    algo = engine.create({**CONDOR, "lots": 2})
    engine.set_enabled(algo["id"], True)
    engine.evaluate(algo["id"])
    edited = engine.update(algo["id"], {**CONDOR, "lots": 2, "steps": STEPS})
    assert edited["steps"]["unit"] == "rs" and edited["book"]
