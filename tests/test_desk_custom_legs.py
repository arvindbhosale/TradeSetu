"""Your own option legs: preset strikes changed, custom strategies, whole-position stoploss and target in rupees."""

import pandas as pd
import pytest

from desk.optstrat import analyse, legs_for, own_legs, run_structure

from .test_desk_fno_algos import CONDOR, NEAR, Clock, setup
from .test_desk_optstrat import QTY, walk

pytestmark = pytest.mark.unit

CONDOR_LEGS = [{"side": "SELL", "type": "CE", "strikes": 2, "lots": 1}, {"side": "BUY", "type": "CE", "strikes": 6, "lots": 1},
               {"side": "SELL", "type": "PE", "strikes": -2, "lots": 1}, {"side": "BUY", "type": "PE", "strikes": -6, "lots": 1}]


def test_own_legs_match_the_preset_they_copy():
    assert [x["n"] for x in legs_for("iron_condor", 2, 4)] == [2, 6, -2, -6]
    mine = own_legs(CONDOR_LEGS)
    assert [(x["action"], x["type"], x["n"], x["ratio"], x["series"]) for x in mine] == \
           [(x["action"], x["type"], x["n"], x["ratio"], x["series"]) for x in legs_for("iron_condor", 2, 4)]
    preset = analyse("NIFTY", "iron_condor", spot=24000, iv=0.14, days=7, lot_size=QTY, k=2, w=4)
    custom = analyse("NIFTY", "custom", spot=24000, iv=0.14, days=7, lot_size=QTY, legs=CONDOR_LEGS)
    for key in ("net_rupees", "max_profit", "max_loss", "breakevens", "margin", "pop_pct"):
        assert custom[key] == preset[key], key
    assert custom["name"] == "Your own strategy" and "Sell ATM+2 CE" in custom["legs_text"]
    assert [g["strikes"] for g in custom["legs"]] == [2, 6, -2, -6]


def test_an_uneven_condor_puts_each_strike_where_you_chose():
    legs = [{"side": "SELL", "type": "CE", "strikes": 3, "lots": 1}, {"side": "BUY", "type": "CE", "strikes": 5, "lots": 1},
            {"side": "SELL", "type": "PE", "strikes": -1, "lots": 2}, {"side": "BUY", "type": "PE", "strikes": -8, "lots": 2}]
    r = analyse("NIFTY", "iron_condor", spot=24010, iv=0.14, days=7, lot_size=QTY, legs=legs, premiums=[60, 30, 110, 25])
    assert [g["strike"] for g in r["legs"]] == [24150, 24250, 23950, 23600]
    assert [g["lots"] for g in r["legs"]] == [1, 1, 2, 2]
    assert r["name"] == "Iron condor (your strikes)"
    credit = 60 - 30 + 2 * (110 - 25)
    assert r["net_premium"] == pytest.approx(credit)
    # Worst case is a crash: the put spread 350 wide × 2 lots, less the credit.
    assert r["max_loss"] == pytest.approx(-(2 * 350 - credit) * QTY)


@pytest.mark.parametrize("legs, fragment", [
    ([], "1 to 8 legs"), ([{"side": "HOLD", "type": "CE"}], "Buy or Sell"), ([{"side": "BUY", "type": "XX"}], "Call"),
    ([{"side": "BUY", "type": "CE", "strikes": 61}], "strikes from ATM"), ([{"side": "BUY", "type": "CE", "strikes": 1.5}], "whole"),
    ([{"side": "BUY", "type": "CE", "lots": 0}], "lots"), ([{"side": "BUY", "type": "FUT"}], "at least one option"),
    ([{"side": "BUY", "type": "CE", "expiry": "far"}], "near expiry"), ([{"side": "BUY", "type": "CE", "strikes": True}], "whole"),
])
def test_bad_legs_are_refused(legs, fragment):
    with pytest.raises(ValueError, match=fragment):
        own_legs(legs)


def test_custom_needs_legs():
    with pytest.raises(ValueError, match="custom with your own legs"):
        legs_for("custom", 2, 4)


def test_backtest_with_own_legs_matches_the_preset_and_names_them():
    bars, vix = walk(), pd.Series(14.0, index=walk().index)
    kw = {"mode": "positional", "lot_size": QTY, "capital": 5_000_000, "weekdays": (1,), "min_days": 1, "slippage_pct": 0}
    preset = run_structure("NIFTY", bars, vix, structure="iron_condor", k=2, w=4, **kw)
    custom = run_structure("NIFTY", bars, vix, structure="custom", legs=CONDOR_LEGS, **kw)
    assert [t["pnl"] for t in custom["trades"]] == [t["pnl"] for t in preset["trades"]]
    assert custom["strategy"]["name"].startswith("Your own strategy")
    assert custom["legs_text"] == "Sell ATM+2 CE, Buy ATM+6 CE, Sell ATM−2 PE, Buy ATM−6 PE"
    with pytest.raises(ValueError, match="bullish or bearish preset"):
        run_structure("NIFTY", bars, vix, structure="custom", legs=CONDOR_LEGS, signal=pd.Series(1, index=bars.index),
                      **{**kw, "mode": "signal"})


def test_rupee_stoploss_and_target_close_the_whole_position():
    bars, vix = walk(vol=0.02), pd.Series(14.0, index=walk().index)
    kw = {"structure": "short_straddle", "mode": "positional", "lot_size": QTY, "capital": 5_000_000, "weekdays": (1,), "min_days": 3,
              "slippage_pct": 0}
    free = run_structure("NIFTY", bars, vix, **kw)
    worst = min(t["pnl"] for t in free["trades"])
    assert worst < -6000                                         # the stop below has something to cut
    stopped = run_structure("NIFTY", bars, vix, stop_rs=3000, **kw)
    hits = [t for t in stopped["trades"] if t["exit"] == "stoploss in ₹"]
    assert hits and stopped["params"]["stop_rs"] == 3000
    # Checked at each day's close, so a loss can run past the stop on the day it is hit; never by more than a day's move.
    assert min(t["pnl"] for t in stopped["trades"]) > worst
    target = run_structure("NIFTY", bars, vix, target_rs=1500, **kw)
    wins = [t for t in target["trades"] if t["exit"] == "target in ₹"]
    assert wins and all(t["pnl"] > 0 for t in wins)
    with pytest.raises(ValueError, match="10 crore"):
        run_structure("NIFTY", bars, vix, stop_rs=-1, **kw)


def test_api_backtest_and_payoff_take_own_legs(tmp_path):
    pytest.importorskip("fastapi")
    pytest.importorskip("mcp")
    from fastapi.testclient import TestClient

    from desk.server import create_app
    from desk.settings import Settings

    def fetch(symbol, interval="1d"):
        if symbol == "^INDIAVIX":
            return walk(300, vol=0.0, drift=0.0, base=14.0, start="2025-06-02")
        return walk(300, start="2025-06-02")

    app = create_app(settings=Settings(tmp_path / "settings.json"), token="t", fetch=fetch)
    with TestClient(app, headers={"X-Desk-Token": "t"}) as client:
        state = client.get("/api/state").json()
        assert any(s["key"] == "custom" and s["custom"] for s in state["structures"])
        spot = client.get("/api/options/spot?underlying=NIFTY").json()
        assert spot["spot"] > 0 and spot["atm"] % 50 == 0 and spot["step"] == 50
        r = client.post("/api/options/payoff", json={"structure": "custom", "spot": 24000, "iv_pct": 14, "days": 5,
                                                     "legs": CONDOR_LEGS}).json()
        assert [g["strike"] for g in r["legs"]] == [24100, 24300, 23900, 23700], r.get("error")
        far = client.post("/api/options/payoff", json={"structure": "custom", "spot": 24000, "iv_pct": 14, "days": 3, "legs": [
            {"side": "SELL", "type": "CE", "strikes": 0}, {"side": "BUY", "type": "CE", "strikes": 0, "expiry": "far"}]}).json()
        assert far["far_days"] > 3, far.get("error")
        assert "custom with your own legs" in client.post("/api/options/payoff", json={"structure": "custom", "spot": 24000}).json()["error"]
        body = {"structure": "custom", "legs": CONDOR_LEGS, "underlying": "NIFTY", "mode": "positional", "start": "2025-07-01",
                "end": "2026-06-30", "weekdays": [1], "premiums": "model", "stop_rs": 4000, "target_rs": 2000, "capital": 2_000_000}
        bt = client.post("/api/backtest/structure", json=body).json()
        assert bt.get("trades"), bt.get("error")
        assert bt["params"]["stop_rs"] == 4000 and bt["legs"][0] == {"side": "SELL", "type": "CE", "strikes": 2, "lots": 1, "expiry": "near"}
    app.state.kite.disconnect()


def test_an_algo_trades_your_own_legs_and_keeps_them_on_edit(tmp_path):
    clock = Clock("2026-11-17 09:30")
    engine, desk, prices = setup(tmp_path, clock)
    legs = [{"side": "SELL", "type": "CE", "strikes": 3}, {"side": "BUY", "type": "CE", "strikes": 7},
            {"side": "SELL", "type": "PE", "strikes": -5}, {"side": "BUY", "type": "PE", "strikes": -9}]
    algo = engine.create({**CONDOR, "name": "Mine", "structure": "custom", "legs": legs})
    assert algo["structure_name"] == "Your own strategy" and not algo["naked"]
    assert algo["legs_text"] == "Sell ATM+3 CE, Buy ATM+7 CE, Sell ATM−5 PE, Buy ATM−9 PE"
    engine.set_enabled(algo["id"], True)
    clock.set("2026-11-17 09:35")
    book = engine.evaluate(algo["id"])["algo"]["book"]
    assert book["near"] == NEAR
    assert {(g["type"], g["strike"], g["action"]) for g in book["legs"]} == \
           {("CE", 25150.0, -1), ("CE", 25350.0, 1), ("PE", 24750.0, -1), ("PE", 24550.0, 1)}
    other = engine.create({**CONDOR, "name": "Mine too", "structure": "iron_condor", "legs": legs})
    assert other["structure_name"] == "Iron condor (your strikes)"
    edited = engine.update(other["id"], {**CONDOR, "max_loss": 3000})       # the edit form sends no legs
    assert edited["legs"] == other["legs"] and edited["max_loss"] == 3000
    assert engine.update(other["id"], {**CONDOR, "structure": "short_straddle"})["legs"] is None
    naked = engine.create({**CONDOR, "name": "Naked", "structure": "custom", "legs": legs[:1]})
    assert naked["naked"]
    from desk.fnoalgo import AlgoError
    with pytest.raises(AlgoError, match="made from a backtest"):
        engine.create({**CONDOR, "name": "Nolegs", "structure": "custom"})
