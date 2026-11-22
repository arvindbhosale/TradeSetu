"""Option strategies: the added structures, the payoff analyser and the Greeks."""

import math

import numpy as np
import pandas as pd
import pytest

from desk.options import bs_greeks, bs_price
from desk.optstrat import STRUCTURES, analyse, estimate_margin, leg_specs, run_structure

pytestmark = pytest.mark.unit
QTY = 65


def walk(n=500, seed=9, start="2024-01-01", vol=0.012, base=24000.0, drift=0.0003):
    rng = np.random.default_rng(seed)
    close = base * np.exp(np.cumsum(rng.normal(drift, vol, n)))
    open_ = np.r_[close[0], close[:-1]]
    return pd.DataFrame({"Open": open_, "High": np.maximum(open_, close) * 1.004, "Low": np.minimum(open_, close) * 0.996,
                         "Close": close, "Volume": 1e5}, index=pd.bdate_range(start, periods=n))


def given(structure, premiums, **kw):
    return analyse("NIFTY", structure, spot=24000, iv=0.14, days=7, lot_size=QTY, premiums=premiums, **kw)


def test_every_structure_has_valid_legs_and_names():
    for key, spec in STRUCTURES.items():
        assert spec["name"] and spec["about"] and spec["markets"], key
        for leg in leg_specs(key):
            assert leg["action"] in (1, -1) and leg["type"] in ("CE", "PE", "FUT") and leg["ratio"] >= 1, key
            assert leg["series"] in ("near", "far"), key
    for key in ("short_guts", "long_guts", "long_call_butterfly", "long_put_butterfly", "call_ratio_spread", "put_ratio_spread",
                "call_ratio_backspread", "put_ratio_backspread", "long_call_calendar", "long_put_calendar", "call_diagonal",
                "put_diagonal", "reverse_iron_condor", "reverse_iron_fly", "long_straddle_strap", "long_straddle_strip",
                "collar", "synthetic_long", "synthetic_short", "jade_lizard", "short_call_butterfly"):
        assert key in STRUCTURES


def test_iron_condor_textbook_figures():
    # Sell 24100 CE @100, buy 24300 CE @40, sell 23900 PE @90, buy 23700 PE @30: credit 120, wing 200.
    r = given("iron_condor", [100, 40, 90, 30], k=2, w=4)
    assert [g["strike"] for g in r["legs"]] == [24100, 24300, 23900, 23700]
    assert r["net_premium"] == 120 and r["credit"]
    assert r["max_profit"] == pytest.approx(120 * QTY)
    assert r["max_loss"] == pytest.approx(-(200 - 120) * QTY)
    assert r["breakevens"] == pytest.approx([23780, 24220], abs=0.01)
    assert not r["unlimited_loss"] and not r["unlimited_profit"]
    assert r["margin"] == pytest.approx(200 * QTY, abs=1)          # the wing; the credit pays for the bought wings


def test_short_straddle_and_long_straddle():
    r = given("short_straddle", [200, 180])
    assert r["breakevens"] == pytest.approx([23620, 24380], abs=0.01)
    assert r["max_profit"] == pytest.approx(380 * QTY) and r["unlimited_loss"] and r["max_loss"] is None
    assert r["greeks"]["theta"] > 0 and r["greeks"]["vega"] < 0
    long = given("long_straddle", [200, 180])
    assert long["unlimited_profit"] and long["max_loss"] == pytest.approx(-380 * QTY)
    assert long["margin"] == pytest.approx(380 * QTY, abs=1)


def test_guts_use_in_the_money_strikes_and_need_a_distance():
    r = given("long_guts", [300, 290], k=2)
    call, put = r["legs"]
    assert call["type"] == "CE" and call["strike"] == 23900 and put["type"] == "PE" and put["strike"] == 24100
    # Debit 590 for 200 of intrinsic: max loss 390 between the strikes, breakevens 23900-390 and 24100+390.
    assert r["max_loss"] == pytest.approx(-390 * QTY)
    assert r["breakevens"] == pytest.approx([23510, 24490], abs=0.01)
    with pytest.raises(ValueError, match="at least 1 strike"):
        given("short_guts", [300, 290], k=0)


def test_butterflies_and_ratio_spreads():
    fly = given("long_call_butterfly", [330, 200, 110], w=4)          # 23800 / 2×24000 / 24200
    assert [g["lots"] for g in fly["legs"]] == [1, 2, 1]
    debit = 330 - 2 * 200 + 110
    assert fly["net_premium"] == -debit
    assert fly["max_profit"] == pytest.approx((200 - debit) * QTY) and fly["max_loss"] == pytest.approx(-debit * QTY)
    assert fly["breakevens"] == pytest.approx([23800 + debit, 24200 - debit], abs=0.01)
    assert fly["margin"] == pytest.approx(debit * QTY, abs=1)
    ratio = given("call_ratio_spread", [200, 110], w=4)                # buy 24000, sell 2×24200: credit 20
    assert ratio["unlimited_loss"] and ratio["max_profit"] == pytest.approx((200 + 20) * QTY)
    assert ratio["breakevens"] == pytest.approx([24420], abs=0.01)
    back = given("put_ratio_backspread", [180, 80], w=4)               # sell 24000 PE, buy 2×23800 PE: credit 20
    assert back["max_loss"] == pytest.approx(-(200 - 20) * QTY)
    assert back["breakevens"] == pytest.approx([23800 - 180, 24000 - 20], abs=0.01)
    assert back["max_profit"] > 20 * QTY * 100                         # large on a crash


def test_calendar_values_the_far_leg_at_the_near_expiry():
    r = analyse("NIFTY", "long_call_calendar", spot=24000, iv=0.14, days=7, far_days=14, lot_size=QTY)
    near, far = r["legs"]
    assert near["side"] == "SELL" and far["side"] == "BUY" and near["strike"] == far["strike"] == 24000
    assert far["premium"] > near["premium"] and not r["credit"]
    best = max(r["payoff"], key=lambda p: p["expiry"])
    assert abs(best["spot"] - 24000) <= 100                           # most at the strike
    remaining = bs_price(24000, 24000, 7 / 365, 0.14, "CE")
    assert best["expiry"] == pytest.approx((remaining - (far["premium"] - near["premium"])) * QTY, rel=0.02)
    assert len(r["breakevens"]) == 2 and r["max_loss"] < 0
    assert r["margin"] == pytest.approx(far["premium"] * QTY, abs=1)  # no offset for the near short
    with pytest.raises(ValueError, match="after the near"):
        analyse("NIFTY", "long_call_calendar", spot=24000, iv=0.14, days=7, far_days=5)


def test_synthetics_and_collar():
    long = analyse("NIFTY", "synthetic_long", spot=24000, iv=0.14, days=7, lot_size=QTY)
    assert long["greeks"]["delta"] == pytest.approx(QTY, rel=0.01) and long["unlimited_profit"]
    collar = analyse("NIFTY", "collar", spot=24000, iv=0.14, days=7, k=2, lot_size=QTY)
    assert collar["max_profit"] is not None and collar["max_loss"] is not None
    assert collar["max_loss"] > -(24000 - 23900 + 300) * QTY


def test_greeks_match_finite_differences():
    s, k, t, v = 24000.0, 24100.0, 10 / 365, 0.15
    for kind in ("CE", "PE"):
        g = bs_greeks(s, k, t, v, kind)
        ds = 1.0
        delta = (bs_price(s + ds, k, t, v, kind) - bs_price(s - ds, k, t, v, kind)) / (2 * ds)
        gamma = (bs_price(s + ds, k, t, v, kind) - 2 * bs_price(s, k, t, v, kind) + bs_price(s - ds, k, t, v, kind)) / ds ** 2
        vega = (bs_price(s, k, t, v + 0.005, kind) - bs_price(s, k, t, v - 0.005, kind)) / 1.0
        theta = bs_price(s, k, t - 1 / 365, v, kind) - bs_price(s, k, t, v, kind)
        assert g["delta"] == pytest.approx(delta, abs=1e-4)
        assert g["gamma"] == pytest.approx(gamma, rel=1e-3)
        assert g["vega"] == pytest.approx(vega, rel=1e-3)
        assert g["theta"] == pytest.approx(theta, rel=0.05)
    assert bs_greeks(s, k, 0, v, "CE")["delta"] == 0.0 and bs_greeks(s, 23000, 0, v, "PE")["delta"] == 0.0


def test_probability_of_profit_adds_up():
    a = analyse("NIFTY", "long_call_butterfly", spot=24000, iv=0.14, days=7, lot_size=QTY)
    b = analyse("NIFTY", "short_call_butterfly", spot=24000, iv=0.14, days=7, lot_size=QTY)
    assert a["pop_pct"] + b["pop_pct"] == pytest.approx(100, abs=0.2)


def test_margin_counts_ratio_legs_as_lots():
    legs = [{"action": 1, "type": "CE", "strike": 24000, "ratio": 1, "entry": 200},
            {"action": -1, "type": "CE", "strike": 24200, "ratio": 2, "entry": 110}]
    naked = estimate_margin(legs, 24000, QTY, 12)
    assert naked >= 0.12 * 24000 * QTY                                 # one of the two sold calls is uncovered


def test_every_structure_backtests_and_calendars_roll_the_far_leg():
    bars = walk()
    vix = pd.Series(14.0, index=bars.index)
    for key in STRUCTURES:
        r = run_structure("NIFTY", bars, vix, structure=key, mode="positional", lot_size=QTY, capital=5_000_000,
                          weekdays=(1,), min_days=1, k=2, w=4, slippage_pct=0)
        assert r["trades"], key
        assert all(math.isfinite(t["pnl"]) for t in r["trades"]), key
    cal = run_structure("NIFTY", bars, vix, structure="long_call_calendar", mode="positional", lot_size=QTY,
                        capital=5_000_000, weekdays=(1,), min_days=1)
    first = cal["trades"][0]["contract"]
    assert "(" in first and first.count("CE") == 2                     # the far leg shows its own expiry
    # A long butterfly never loses more than its debit plus charges.
    fly = run_structure("NIFTY", bars, vix, structure="long_call_butterfly", mode="positional", lot_size=QTY,
                        capital=5_000_000, weekdays=(1,), min_days=1, w=4, slippage_pct=0)
    for t in fly["trades"]:
        assert t["pnl"] >= t["entry_price"] * QTY - 400                  # entry_price is −debit per unit
    with pytest.raises(ValueError, match="at least 1 strike"):
        run_structure("NIFTY", bars, vix, structure="long_guts", mode="positional", lot_size=QTY, k=0)


def test_api_payoff(tmp_path):
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
        cal = next(s for s in state["structures"] if s["key"] == "long_call_calendar")
        assert cal["calendar"] and not cal["uses_k"]
        r = client.post("/api/options/payoff", json={"underlying": "NIFTY", "structure": "iron_condor", "k": 2, "w": 4}).json()
        assert r.get("payoff"), r.get("error")
        assert r["iv_pct"] == pytest.approx(14.0) and len(r["breakevens"]) == 2 and r["max_loss"] < 0
        r = client.post("/api/options/payoff", json={"underlying": "NIFTY", "structure": "long_put_calendar",
                                                     "spot": 24000, "iv_pct": 15, "days": 3}).json()
        assert r["far_days"] > 3 and r["spot"] == 24000, r.get("error")
        r = client.post("/api/options/payoff", json={"underlying": "BANKNIFTY", "structure": "iron_condor", "spot": 52000}).json()
        assert "only monthly" in r["error"]
        r = client.post("/api/options/payoff", json={"structure": "short_straddle", "spot": 24000, "iv_pct": 14, "days": 5,
                                                     "premiums": [150, None]}).json()
        assert r["legs"][0]["premium"] == 150 and r["legs"][0]["source"] == "given" and r["legs"][1]["source"] == "model"
        assert "one value per leg" in client.post("/api/options/payoff", json={"structure": "short_straddle", "spot": 24000,
                                                                               "iv_pct": 14, "premiums": [1]}).json()["error"]
    app.state.kite.disconnect()


def test_where_the_maximum_profit_and_loss_sit_on_the_index():
    condor = given("iron_condor", [100, 40, 90, 30], k=2, w=4)      # short 23900/24100, wings 23700/24300
    assert condor["max_profit_at"] == [{"from": 23900, "to": 24100}]
    assert condor["max_loss_at"] == [{"from": 0, "to": 23700}, {"from": 24300, "to": None}]
    fly = given("long_call_butterfly", [330, 200, 110], w=4)          # peak at 24000
    assert fly["max_profit_at"] == [{"from": 24000, "to": 24000}]
    straddle = given("short_straddle", [200, 180])
    assert straddle["unlimited_loss"] and straddle["max_loss_at"] == []
    assert straddle["max_profit_at"] == [{"from": 24000, "to": 24000}]
    back = given("put_ratio_backspread", [180, 80], w=4)               # profit grows all the way down to a crash
    assert back["max_profit_at"] == [{"from": 0, "to": 0}] and back["max_loss_at"] == [{"from": 23800, "to": 23800}]
    bull = given("bull_call_spread", [200, 110], w=4)                  # buy 24000 CE, sell 24200 CE
    assert bull["max_profit_at"] == [{"from": 24200, "to": None}] and bull["max_loss_at"] == [{"from": 0, "to": 24000}]
