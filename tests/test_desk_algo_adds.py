"""Algos add to winners: stock algos buy more shares, F&O algos add lots, with the money checked first and the
stoploss of the whole position moved after each add."""

import copy

import pandas as pd
import pytest

pytest.importorskip("fastapi")
pytest.importorskip("mcp")

import tradingagents.dataflows.config as config_module
from desk.algo import AlgoError
from tests.test_desk_algo_steps import bought
from tests.test_desk_fno_algo_steps import STEPS as FNO_STEPS, decay
from tests.test_desk_fno_algos import CONDOR, Clock, journal, setup
from tests.test_desk_positional import SPEC

pytestmark = pytest.mark.unit
ADDS = {"unit": "pct", "adds": [{"at": 3, "size": 50}], "stop_after": "risk"}


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("BHARAT_DESK_HOME", str(tmp_path / "desk"))
    saved = copy.deepcopy(config_module._config)
    config_module.set_config({"default_exchange": "NSE"})
    yield
    config_module._config = saved


# -- stock algos -------------------------------------------------------------------------------------------------------

def test_bad_adds_are_refused_in_plain_words(tmp_path):
    engine, _, _ = bought(tmp_path, SPEC)
    with pytest.raises(AlgoError, match="higher than the one before"):
        engine.create({**SPEC, "name": "Two", "adds": {"adds": [{"at": 4, "size": 20}, {"at": 2, "size": 20}]}})
    with pytest.raises(AlgoError, match="for option strategies"):
        engine.create({**SPEC, "name": "Two", "adds": {"adds": [{"at": 4, "size": 20}], "scope": "leg"}})


def test_a_positional_algo_adds_shares_and_places_the_gtt_again_for_all_of_them(tmp_path):
    spec = {**SPEC, "exits": {"unit": "pct", "stop_loss": 4}, "adds": ADDS}
    engine, algo, at = bought(tmp_path, spec)
    held, entry = algo["position"], algo["average_price"]
    # The budget leaves room for the add at its level: the first buy is smaller than the whole budget would buy.
    assert held * entry <= algo["budget"] / (1 + 0.5 * 1.03) + entry
    assert algo["first_price"] == entry and algo["first_qty"] == held and algo["risk0"] > 0
    first_gtt = engine.desk.paper.gtt(algo["protect"])

    algo = at("10:00", round(entry * 1.031, 2))                         # past +3%: half as many shares again
    added = held // 2
    assert algo["position"] == held + added and algo["adds_done"] == 1
    assert algo["average_price"] > entry and algo["adds_made"][0]["quantity"] == added
    stop = algo["add_stop"]
    # A fall to the moved stoploss loses what the first buy's 4% stoploss planned, no more.
    assert algo["position"] * (algo["average_price"] - stop) == pytest.approx(algo["risk0"], abs=algo["position"] * 0.02)
    gtt = engine.desk.paper.gtt(algo["protect"])
    assert gtt["quantity"] == algo["position"] and (gtt.get("stop") or gtt.get("trigger")) == pytest.approx(stop, abs=0.05)
    assert engine.desk.paper.gtt(first_gtt["id"])["status"] == "cancelled"
    assert any("Added" in e["message"] and "Stoploss of every share moved" in e["message"] for e in algo["log"])

    algo = at("10:05", round(entry * 1.05, 2))                          # one add only
    assert algo["position"] == held + added


def test_an_intraday_algo_does_not_add_with_the_kill_switch_on_or_short_of_money(tmp_path):
    spec = {**SPEC, "name": "Intra", "trade_type": "intraday", "exits": {"unit": "pct", "stop_loss": 3}, "adds": ADDS}
    engine, algo, at = bought(tmp_path, spec)
    held, entry = algo["position"], algo["average_price"]
    engine.desk.settings.update({"kill_switch": True})
    algo = at("10:00", round(entry * 1.031, 2))
    assert algo["position"] == held and algo["adds_done"] == 0
    assert any("kill switch" in e["message"] for e in algo["log"])
    engine.desk.settings.update({"kill_switch": False})
    engine.desk.paper._state["cash"] = 10.0                             # the paper cash cannot pay for the add
    algo = at("10:02", round(entry * 1.032, 2))
    assert algo["position"] == held and algo["adds_done"] == 1
    assert any("Add 1 skipped" in e["message"] for e in algo["log"])


def test_no_add_after_a_profit_step(tmp_path):
    spec = {**SPEC, "name": "Steps", "steps": {"unit": "pct", "steps": [{"at": 2, "size": 50}], "stop_after": "cost"},
            "adds": ADDS}
    engine, algo, at = bought(tmp_path, spec)
    held, entry = algo["position"], algo["average_price"]
    algo = at("10:00", round(entry * 1.021, 2))
    assert algo["position"] == held - held // 2 and algo["steps_done"] == 1
    algo = at("10:05", round(entry * 1.04, 2))
    assert algo["position"] == held - held // 2 and not algo["adds_done"]


# -- F&O algos ---------------------------------------------------------------------------------------------------------

FNO_ADDS = {"unit": "rs", "adds": [{"at": 1000, "size": 1}], "stop_after": "risk"}


def test_a_paper_condor_adds_a_lot_of_every_leg_hedges_first(tmp_path):
    clock = Clock("2026-11-17 09:35")
    engine, desk, prices = setup(tmp_path, clock)
    algo = engine.create({**CONDOR, "lots": 2, "adds": FNO_ADDS})
    assert "add to winners: 1 lot at ₹1,000 profit a lot" in algo["log"][-1]["message"]
    engine.set_enabled(algo["id"], True)
    book = engine.evaluate(algo["id"])["algo"]["book"]
    assert all(g["first"] == g["avg"] for g in book["legs"])
    opened = len(journal(desk))
    clock.set("2026-11-17 10:30")
    decay(prices, book, 0.8)                                    # about ₹1,300 a lot: the add
    out = engine.evaluate(algo["id"])
    book = out["algo"]["book"]
    assert book["phase"] == "open" and book["lots_open"] == 3 and book["adds_done"] == 1
    assert all(g["open_qty"] == 195 for g in book["legs"])
    added = journal(desk)[opened:]
    assert [(r["side"], r["quantity"]) for r in added] == [("BUY", 65), ("BUY", 65), ("SELL", 65), ("SELL", 65)]
    assert [r["tradingsymbol"] for r in added[:2]] == [g["tradingsymbol"] for g in book["legs"] if g["action"] > 0]
    assert book["adds"][0]["lots"] == 1 and book["adds"][0]["time"] == "2026-11-17 10:30"
    assert book["floor"] == 0.0                                # paper without a maximum loss: breakeven
    # The profit falls back below zero: every lot closes, not just the added one.
    clock.set("2026-11-17 11:00")
    for g in book["legs"]:
        prices.override[g["instrument"]] = round(g["first"] * 1.1, 2)
    out = engine.evaluate(algo["id"])
    assert out["algo"]["book"] is None
    trade = out["algo"]["history"][-1]
    assert trade["reason"].startswith("stoploss moved after adding") and trade["adds"][0]["lots"] == 1


def test_an_add_is_skipped_when_the_margin_is_short(tmp_path):
    clock = Clock("2026-11-17 09:35")
    engine, desk, prices = setup(tmp_path, clock)
    algo = engine.create({**CONDOR, "lots": 2, "adds": FNO_ADDS})
    engine.set_enabled(algo["id"], True)
    book = engine.evaluate(algo["id"])["algo"]["book"]
    desk.paper._state["cash"] = book["margin"] + 100           # nothing over this algo's own margin
    clock.set("2026-11-17 10:30")
    decay(prices, book, 0.8)
    out = engine.evaluate(algo["id"])
    assert out["algo"]["book"]["lots_open"] == 2 and out["algo"]["book"]["adds_done"] == 1
    assert any("skipped" in e["message"] and "available" in e["message"] for e in out["algo"]["log"])


def test_adds_to_one_bought_leg_only_and_never_a_sold_one(tmp_path):
    clock = Clock("2026-11-17 09:35")
    engine, desk, prices = setup(tmp_path, clock)
    with pytest.raises(AlgoError, match="only a bought option leg"):
        engine.create({**CONDOR, "name": "Straddle", "structure": "short_straddle", "k": 0,
                       "adds": {**FNO_ADDS, "scope": "leg"}})
    algo = engine.create({**CONDOR, "lots": 1, "adds": {"unit": "pct", "adds": [{"at": 50, "size": 2}], "scope": "leg"}})
    engine.set_enabled(algo["id"], True)
    book = engine.evaluate(algo["id"])["algo"]["book"]
    call = next(g for g in book["legs"] if g["action"] > 0 and g["type"] == "CE")
    opened = len(journal(desk))
    clock.set("2026-11-17 10:30")
    prices.override[call["instrument"]] = round(call["avg"] * 1.6, 2)  # the bought call is up 60%
    book = engine.evaluate(algo["id"])["algo"]["book"]
    added = journal(desk)[opened:]
    assert [(r["side"], r["tradingsymbol"], r["quantity"]) for r in added] == [("BUY", call["tradingsymbol"], 130)]
    grown = next(g for g in book["legs"] if g["tradingsymbol"] == call["tradingsymbol"])
    assert grown["open_qty"] == 195 and book["lots_open"] == 1
    assert all(g["open_qty"] == 65 for g in book["legs"] if g is not grown)
    assert book["adds"][0]["leg"] == call["tradingsymbol"]


def test_futures_algo_adds_lots_and_steps_stop_further_adds(tmp_path):
    import numpy as np

    clock = Clock("2026-11-17 09:35")
    engine, desk, prices = setup(tmp_path, clock, max_order_value=5_000_000.0)
    days = pd.bdate_range(end="2026-11-16", periods=120)
    closes = np.r_[np.linspace(26000, 24000, 80), np.linspace(24000, 25500, 40)]
    engine.fetch = lambda symbol, interval="1d": pd.DataFrame(
        {"Open": closes, "High": closes * 1.003, "Low": closes * 0.997, "Close": closes, "Volume": 1e6}, index=days)
    desk.paper._state["cash"] = 5_000_000.0
    algo = engine.create({"name": "Nifty fut", "kind": "futures", "underlying": "NIFTY", "direction": "long",
                          "strategy": "sma_cross", "strategy_params": {"fast": 5, "slow": 20}, "lots": 1,
                          "mode": "paper", "entry_time": "09:35", "exit_time": "15:10",
                          "adds": {"unit": "points", "adds": [{"at": 100, "size": 1}, {"at": 200, "size": 1}]},
                          "steps": {"unit": "points", "steps": [{"at": 150, "size": 1}]}})
    engine.set_enabled(algo["id"], True)
    engine.evaluate(algo["id"])
    engine.get(algo["id"])["ready"] = True
    book = engine.evaluate(algo["id"])["algo"]["book"]
    fut = book["legs"][0]
    first = fut["avg"]
    prices.override[fut["instrument"]] = first + 120
    book = engine.evaluate(algo["id"])["algo"]["book"]
    assert book["lots_open"] == 2 and book["legs"][0]["open_qty"] == 130 and book["legs"][0]["first"] == first
    prices.override[fut["instrument"]] = first + 160            # the profit step: one lot closed
    book = engine.evaluate(algo["id"])["algo"]["book"]
    assert book["lots_open"] == 1 and book["steps_done"] == 1
    prices.override[fut["instrument"]] = first + 250            # past the second add: none after a step
    book = engine.evaluate(algo["id"])["algo"]["book"]
    assert book["lots_open"] == 1 and book["adds_done"] == 1


def test_adds_can_change_while_legs_are_open(tmp_path):
    clock = Clock("2026-11-17 09:35")
    engine, _, _ = setup(tmp_path, clock)
    algo = engine.create({**CONDOR, "lots": 2, "steps": FNO_STEPS})
    engine.set_enabled(algo["id"], True)
    engine.evaluate(algo["id"])
    edited = engine.update(algo["id"], {**CONDOR, "lots": 2, "steps": FNO_STEPS, "adds": FNO_ADDS})
    assert edited["adds"]["unit"] == "rs" and edited["book"]


def test_a_live_add_sends_the_sold_legs_only_after_the_hedges_fill(tmp_path):
    from desk.fnoalgo import arm_phrase
    from tests.test_desk_fno_algos import Market, StubKite

    clock = Clock("2026-11-17 09:35")
    kite = StubKite(Market())
    engine, desk, prices = setup(tmp_path, clock, kite=kite, live_enabled=True)
    kite.prices = prices
    algo = engine.create({**CONDOR, "lots": 1, "mode": "live", "max_loss": 9000, "adds": FNO_ADDS})
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
    assert engine.evaluate(algo["id"])["algo"]["book"]["phase"] == "opening"
    hedges = kite.placed[before:]
    assert [(p["transaction_type"], p["quantity"]) for p in hedges] == [("BUY", 65), ("BUY", 65)]
    for p in hedges:
        kite.fill(p["order_id"])
    engine.evaluate(algo["id"])
    sold = kite.placed[before + 2:]
    assert [(p["transaction_type"], p["quantity"]) for p in sold] == [("SELL", 65), ("SELL", 65)]
    for p in sold:
        kite.fill(p["order_id"])
    book = engine.evaluate(algo["id"])["algo"]["book"]
    assert book["phase"] == "open" and book["lots_open"] == 2 and all(g["open_qty"] == 130 for g in book["legs"])
    assert book["floor"] == -9000                               # the rupee loss stays at the maximum loss
