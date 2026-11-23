"""Choosing which expiry an option strategy trades: this one, the next one, or up to three ahead."""

import pandas as pd
import pytest

from desk.optstrat import analyse, expiry_after, expiry_ahead_of, expiry_shift, run_structure

from .test_desk_optstrat import QTY, walk

pytestmark = pytest.mark.unit

STRADDLE = [{"side": "SELL", "type": "CE", "strikes": 0}, {"side": "SELL", "type": "PE", "strikes": 0}]
FAR_STRADDLE = [{**g, "expiry": "far"} for g in STRADDLE]


def test_all_legs_on_the_far_expiry_mean_one_expiry_later():
    legs, ahead = expiry_shift(FAR_STRADDLE, 0)
    assert ahead == 1 and all(g["expiry"] == "near" for g in legs)
    calendar = [STRADDLE[0], {**STRADDLE[0], "side": "BUY", "expiry": "far"}]
    assert expiry_shift(calendar, 2) == (calendar, 2)                   # a calendar keeps its two expiries
    with_future = [{"side": "BUY", "type": "FUT"}, {"side": "SELL", "type": "CE", "expiry": "far"}]
    assert expiry_shift(with_future, 0)[1] == 1                         # futures do not count as a near leg
    assert expiry_shift(None, 1) == (None, 1)
    for bad in (-1, 4, 1.5, True, "1"):
        with pytest.raises(ValueError, match="Expiry must be"):
            expiry_shift(STRADDLE, bad)


def test_expiry_ahead_steps_through_the_expiries():
    moment = pd.Timestamp("2026-11-23 10:00")                         # a Monday; NIFTY weeklies are on Tuesday
    near = expiry_after("NIFTY", moment, "weekly", 0.0)
    assert expiry_ahead_of("NIFTY", moment, "weekly", 0.0, 0) == near
    assert (expiry_ahead_of("NIFTY", moment, "weekly", 0.0, 2) - near).days == 14
    month = expiry_after("NIFTY", moment, "monthly", 0.0)
    nxt = expiry_ahead_of("NIFTY", moment, "monthly", 0.0, 1)
    assert nxt > month and nxt.month == (month.month % 12) + 1


def test_backtest_on_the_next_expiry():
    bars, vix = walk(), pd.Series(14.0, index=walk().index)
    kw = {"structure": "custom", "mode": "positional", "lot_size": QTY, "capital": 5_000_000, "weekdays": (1,), "min_days": 1,
          "slippage_pct": 0}
    this = run_structure("NIFTY", bars, vix, legs=STRADDLE, **kw)
    nxt = run_structure("NIFTY", bars, vix, legs=STRADDLE, expiry_ahead=1, **kw)
    assert this["trades"] and nxt["trades"]
    held = lambda r: [pd.Timestamp(t["exit_date"]) - pd.Timestamp(t["entry_date"]) for t in r["trades"]]  # noqa: E731
    assert max(held(nxt)) > max(held(this))                             # held to a later expiry
    assert nxt["params"]["expiry_ahead"] == 1
    # Every leg on "the expiry after" is the same trade as the next expiry, not an error.
    far = run_structure("NIFTY", bars, vix, legs=FAR_STRADDLE, **kw)
    assert [t["pnl"] for t in far["trades"]] == [t["pnl"] for t in nxt["trades"]]
    assert far["params"]["expiry_ahead"] == 1


def test_payoff_with_every_leg_on_the_far_expiry_is_at_that_expiry():
    near = analyse("NIFTY", "custom", spot=24000, iv=0.14, days=12, lot_size=QTY, legs=STRADDLE)
    far = analyse("NIFTY", "custom", spot=24000, iv=0.14, days=5, far_days=12, lot_size=QTY, legs=FAR_STRADDLE)
    assert far["days"] == 12 and far["far_days"] is None
    for key in ("net_rupees", "max_profit", "breakevens", "pop_pct"):
        assert far[key] == near[key], key


def test_api_payoff_and_backtest_take_the_chosen_expiry(tmp_path):
    from fastapi.testclient import TestClient

    from desk.server import create_app
    from desk.settings import Settings

    def fetch(symbol, interval="1d"):
        if symbol == "^INDIAVIX":
            return walk(300, vol=0.0, drift=0.0, base=14.0, start="2025-06-02")
        return walk(300, start="2025-06-02")

    app = create_app(settings=Settings(tmp_path / "settings.json"), token="t", fetch=fetch)
    with TestClient(app, headers={"X-Desk-Token": "t"}) as client:
        body = {"structure": "custom", "spot": 24000, "iv_pct": 14, "legs": STRADDLE, "expiry_kind": "weekly"}
        this = client.post("/api/options/payoff", json=body).json()
        nxt = client.post("/api/options/payoff", json={**body, "expiry_ahead": 1}).json()
        assert 5 < nxt["days"] - this["days"] < 9, nxt.get("error")
        far = client.post("/api/options/payoff", json={**body, "legs": FAR_STRADDLE}).json()
        assert far["days"] == pytest.approx(nxt["days"]) and far["far_days"] is None, far.get("error")
        assert "Expiry must be" in client.post("/api/options/payoff", json={**body, "expiry_ahead": 5}).json()["error"]
        bt = {"structure": "custom", "legs": FAR_STRADDLE, "underlying": "NIFTY", "mode": "positional", "start": "2025-07-01",
              "end": "2026-06-30", "weekdays": [1], "premiums": "model", "capital": 2_000_000}
        r = client.post("/api/backtest/structure", json=bt).json()
        assert r.get("trades") and r["params"]["expiry_ahead"] == 1, r.get("error")
    app.state.kite.disconnect()
