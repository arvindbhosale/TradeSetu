"""Index futures, passkeys, legend strategies, option structures, screens, portfolios, bad-print repair."""

import copy
import sys
from datetime import date

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("fastapi")
pytest.importorskip("mcp")

import tradingagents.dataflows.config as config_module
from desk import market
from desk.charges import futures_charges
from desk.futures import monthly_expiries, run_futures
from desk.optstrat import STRUCTURES, expiry_after, run_structure, weekly_weekday
from desk.portfolio import repair_splits, run_portfolio
from desk.screens import run as run_screen
from desk.settings import Settings
from desk.strategies import STRATEGIES

pytestmark = pytest.mark.unit
FAKE = {"command": sys.executable, "args": ["-m", "desk.dev.fake_kite_mcp"]}


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADESETU_HOME", str(tmp_path / "desk"))
    saved, holidays = copy.deepcopy(config_module._config), set(market.HOLIDAYS)
    config_module.set_config({"default_exchange": "NSE"})
    yield
    config_module._config = saved
    market.set_holidays(holidays)


def walk(n=800, seed=7, start="2022-01-03", vol=0.012, drift=0.0004, base=20000.0):
    rng = np.random.default_rng(seed)
    close = base * np.exp(np.cumsum(rng.normal(drift, vol, n)))
    open_ = np.r_[close[0], close[:-1]]
    return pd.DataFrame({"Open": open_, "High": np.maximum(open_, close) * 1.004, "Low": np.minimum(open_, close) * 0.996,
                         "Close": close, "Volume": 1e5}, index=pd.bdate_range(start, periods=n))


# -- futures ----------------------------------------------------------------------------

def test_monthly_expiries_follow_the_exchange_calendar():
    days = monthly_expiries(date(2025, 7, 1), date(2025, 10, 31))
    assert days[:4] == [date(2025, 7, 31), date(2025, 8, 28), date(2025, 9, 30), date(2025, 10, 28)]   # Thu, Thu, then Tue
    assert monthly_expiries(date(2026, 11, 1), date(2026, 11, 30))[0] == date(2026, 11, 24)
    assert monthly_expiries(date(2026, 3, 1), date(2026, 3, 31), holidays={"2026-03-31"})[0] == date(2026, 3, 30)
    assert monthly_expiries(date(2025, 10, 1), date(2025, 10, 31), exchange="BFO")[0] == date(2025, 10, 30)


def test_futures_charges_match_the_schedule():
    buy, sell = futures_charges("BUY", 75, 24000), futures_charges("SELL", 75, 24000)
    assert buy.brokerage == 20.0 and buy.stt == 0 and sell.stt == pytest.approx(1_800_000 * 0.0005)
    assert buy.stamp_duty == pytest.approx(36.0) and sell.stamp_duty == 0
    assert buy.gst == pytest.approx((20 + 1_800_000 * 0.0000183 + 1.8) * 0.18)


def test_a_long_futures_trade_pays_the_move_times_the_lot():
    bars = walk(60, vol=0.0, drift=0.0)
    bars.loc[bars.index[30]:, ["Open", "High", "Low", "Close"]] *= 1.05            # a 5% step up on day 30
    signal = pd.Series(0, index=bars.index)
    signal.iloc[10:40] = 1
    r = run_futures(bars, signal, underlying="NIFTY", lot_size=65, capital=1_000_000, continuous=False, slippage_pct=0)
    trade = r["trades"][0]
    assert trade["side"] == "LONG" and trade["entry_date"] == bars.index[11].strftime("%Y-%m-%d")
    gross = 65 * (trade["exit_price"] - trade["entry_price"])
    assert gross == pytest.approx(65 * trade["entry_price"] * 0.05, rel=1e-6)
    assert trade["pnl"] == pytest.approx(gross - futures_charges("BUY", 65, trade["entry_price"]).total
                                         - futures_charges("SELL", 65, trade["exit_price"]).total, abs=0.05)
    short = run_futures(bars, signal, underlying="NIFTY", lot_size=65, capital=1_000_000, direction="short", continuous=False,
                        slippage_pct=0)
    assert {t["side"] for t in short["trades"]} == {"SHORT"} and short["trades"][-1]["pnl"] < 0   # short through the rise


def test_rolls_take_out_the_basis_jump_and_charge_a_round_trip():
    bars = walk(80, vol=0.0, drift=0.0, start="2026-08-03")
    expiries = monthly_expiries(date(2026, 8, 1), date(2026, 12, 31))
    for expiry, following in zip(expiries, expiries[1:], strict=False):
        after = bars.index > pd.Timestamp(expiry)
        if after.any():
            # the continuous series steps up to the next month's contract by its carry
            bars.loc[after, ["Open", "High", "Low", "Close"]] += bars["Close"].iloc[0] * 0.07 * (following - expiry).days / 365
    signal = pd.Series(1, index=bars.index)
    r = run_futures(bars, signal, underlying="NIFTY", lot_size=65, capital=1_000_000, continuous=True, slippage_pct=0,
                    carry_pct=7.0)
    assert r["metrics"]["rolls"] >= 1
    final = r["equity"][-1][1]
    # Flat prices apart from the roll step: the only losses are charges, not a phantom gain of the basis.
    per_trip = futures_charges("BUY", 65, 20000).total + futures_charges("SELL", 65, 20000).total
    assert final == pytest.approx(1_000_000 - per_trip * (r["metrics"]["rolls"] + 0.5), abs=per_trip * 0.6)


def test_futures_entries_need_margin_and_intraday_squares_off():
    bars = walk(100)
    signal = STRATEGIES["sma_cross"].positions(bars, {"fast": 5, "slow": 20})
    poor = run_futures(bars, signal, underlying="NIFTY", lot_size=65, capital=10_000)
    assert poor["trades"] == [] and poor["metrics"]["skipped_entries"] > 0
    idx = pd.DatetimeIndex([d + pd.Timedelta(minutes=9 * 60 + 15 + 5 * k) for d in pd.bdate_range("2026-11-02", periods=5) for k in range(75)])
    rng = np.random.default_rng(1)
    close = 24000 * np.exp(np.cumsum(rng.normal(0, 0.001, len(idx))))
    intraday = pd.DataFrame({"Open": close, "High": close * 1.0005, "Low": close * 0.9995, "Close": close, "Volume": 0.0}, index=idx)
    r = run_futures(intraday, pd.Series(1, index=idx), underlying="NIFTY", lot_size=65, capital=1_000_000, intraday=True,
                    continuous=False, square_off="15:15")
    assert len(r["trades"]) == 5 and all(t["exit_date"][11:] <= "15:15" and t["entry_date"][11:] >= "09:30" for t in r["trades"])


# -- strategies ----------------------------------------------------------------------------

def test_every_strategy_is_tagged_and_runs():
    from desk.backtest import BacktestParams, run_strategy

    daily = walk(900)
    idx = pd.DatetimeIndex([d + pd.Timedelta(minutes=9 * 60 + 15 + 5 * k) for d in pd.bdate_range("2026-11-02", periods=5) for k in range(75)])
    close = 24000 * np.exp(np.cumsum(np.random.default_rng(2).normal(0, 0.001, len(idx))))
    intraday = pd.DataFrame({"Open": close, "High": close * 1.0005, "Low": close * 0.9995, "Close": close, "Volume": 1e4}, index=idx)
    legends = 0
    for key, strategy in STRATEGIES.items():
        info = strategy.describe()
        assert info["style"] and set(info["markets"]) <= {"bull", "bear", "sideways"} and info["markets"], key
        legends += bool(info["legend"])
        bars = intraday if strategy.intraday_only else daily
        result = run_strategy(bars, key, None, BacktestParams(intraday=strategy.intraday_only))
        assert set(strategy.positions(bars).unique()) <= {0, 1}
        assert result["metrics"]["total_charges"] >= 0
    assert legends >= 15
    names = " ".join(s.name for s in STRATEGIES.values())
    for person in ("Minervini", "Turtle", "Darvas", "Livermore", "Weinstein", "O'Neil", "Connors", "Raschke", "Williams",
                   "Tudor Jones", "Bollinger", "Crabel", "Buffett"):
        assert person in names


def test_minervini_needs_the_whole_template_and_turtle_honours_its_atr_stop():
    up = walk(400, vol=0.006, drift=0.002, seed=3)
    assert STRATEGIES["minervini"].positions(up).sum() > 0
    down = walk(400, vol=0.006, drift=-0.002, seed=3)
    assert STRATEGIES["minervini"].positions(down).sum() == 0                 # a falling stock never passes
    bars = walk(120, vol=0.0, drift=0.0)
    bars.iloc[40:, :4] *= 1.05                                                 # breakout on day 40
    bars.iloc[50:, :4] *= 0.90                                                 # a fall far beyond 2 × ATR
    held = STRATEGIES["turtle"].positions(bars, {"entry": 20, "exit": 60, "atr_stop": 2.0})
    assert held.iloc[40] == 1 and held.iloc[50] == 0


def test_connors_rsi2_stays_out_below_the_200_day():
    bars = walk(600, seed=4)
    held = STRATEGIES["connors_rsi2"].positions(bars)
    entries = held.index[(held == 1) & (held.shift(1, fill_value=0) == 0)]
    trend = bars["Close"].rolling(200).mean()
    assert len(entries) > 3 and all(bars["Close"][d] > trend[d] for d in entries)


# -- option structures ---------------------------------------------------------------------------

def test_weekly_expiry_days_follow_each_exchange():
    assert weekly_weekday("NIFTY", date(2025, 8, 1)) == 3 and weekly_weekday("NIFTY", date(2025, 10, 1)) == 1
    assert weekly_weekday("SENSEX", date(2024, 6, 1)) == 4 and weekly_weekday("SENSEX", date(2025, 10, 1)) == 3
    stamp = expiry_after("NIFTY", pd.Timestamp("2026-11-16 10:00"), "weekly", 0)
    assert stamp == pd.Timestamp("2026-11-17 15:30")
    assert expiry_after("NIFTY", pd.Timestamp("2026-11-16 10:00"), "monthly", 0) == pd.Timestamp("2026-11-24 15:30")
    assert expiry_after("NIFTY", pd.Timestamp("2026-11-16 10:00"), "weekly", 2) == pd.Timestamp("2026-11-24 15:30")


def test_defined_risk_structures_never_lose_more_than_the_wing():
    bars = walk(500, vol=0.02, seed=9, start="2024-01-01")
    vix = pd.Series(14.0, index=bars.index)
    for key in ("iron_condor", "iron_fly", "bull_put_spread", "bear_call_spread", "bull_call_spread", "bear_put_spread"):
        r = run_structure("NIFTY", bars, vix, structure=key, mode="positional", lot_size=65, capital=2_000_000,
                          weekdays=(1,), min_days=1, w=4, slippage_pct=0)
        assert r["trades"], key
        worst = 4 * 50 * 65 + 400                                              # wing × step × qty, plus charges
        assert min(t["pnl"] for t in r["trades"]) >= -worst, key
        if key in ("iron_condor", "iron_fly", "bull_put_spread", "bear_call_spread"):
            assert r["trades"][0]["entry_price"] > 0                           # a credit
            bought = sum(t["entry_price"] for t in r["trades"]) / len(r["trades"])
            assert r["metrics"]["avg_margin"] <= 4 * 50 * 65 + 10_000          # the larger wing (+ wings bought), not naked
            assert r["metrics"]["avg_margin"] < 0.12 * 20000 * 65 / 2, bought
        else:
            assert r["trades"][0]["entry_price"] < 0                           # a debit


def test_leg_stops_close_one_leg_and_signal_mode_respects_direction():
    days = pd.bdate_range("2026-10-01", periods=20)
    idx = pd.DatetimeIndex([d + pd.Timedelta(minutes=9 * 60 + 15 + 5 * k) for d in days for k in range(75)])
    path = np.r_[np.zeros(20), np.linspace(0, 0.02, 55)]                       # each day: quiet, then a 2% rally
    close = 24000 * np.exp(np.tile(path, len(days)))
    intraday = pd.DataFrame({"Open": close, "High": close * 1.0002, "Low": close * 0.9998, "Close": close, "Volume": 0.0}, index=idx)
    vix = pd.Series(14.0, index=days)
    r = run_structure("NIFTY", intraday, vix, structure="short_straddle", mode="intraday", lot_size=65, capital=5_000_000,
                      leg_sl_pct=30, entry_time="09:20", exit_time="15:15")
    assert r["metrics"]["legs_stopped"] >= len(days) * 0.9                     # the call leg is stopped each day
    assert all(t["legs_stopped"] == 1 for t in r["trades"])                    # the put leg is left on
    daily = walk(300, seed=11, start="2025-06-02")
    signal = STRATEGIES["supertrend"].positions(daily)
    bear = run_structure("NIFTY", daily, pd.Series(14.0, index=daily.index), structure="bear_put_spread", mode="signal",
                         lot_size=65, capital=2_000_000, signal=signal, min_days=1)
    for t in bear["trades"]:
        assert signal.shift(1).fillna(0).loc[t["entry_date"]] == 0             # opened while the strategy was flat
    with pytest.raises(ValueError, match="directional"):
        run_structure("NIFTY", daily, pd.Series(14.0, index=daily.index), structure="iron_condor", mode="signal",
                      lot_size=65, signal=signal)
    with pytest.raises(ValueError, match="only monthly"):
        run_structure("BANKNIFTY", daily, pd.Series(14.0, index=daily.index), structure="iron_condor", mode="positional",
                      lot_size=30, expiry_kind="weekly")
    assert len(STRUCTURES) >= 13


# -- screens and portfolios ------------------------------------------------------------------------

def test_screens_apply_every_rule_and_rank():
    rows = [{"symbol": s, "name": s, "industry": "x"} for s in ("A", "B", "C", "D")]
    figures = {"A": {"roe": 25, "debt_to_equity": 0.1, "profit_margin": 20, "pe": 30, "market_cap": 5e12, "pb": 4,
                     "earnings_growth": 30, "roa": 12, "sector": "Technology"},
               "B": {"roe": 18, "debt_to_equity": 0.3, "profit_margin": 12, "pe": 20, "market_cap": 2e12, "pb": 1.2,
                     "earnings_growth": 25, "roa": 9, "sector": "Industrials", "dividend_yield": 1.0},
               "C": {"roe": 30, "debt_to_equity": 2.0, "profit_margin": 25, "pe": 10, "market_cap": 9e12, "roa": 3,
                     "sector": "Financial Services"},
               "D": None}
    buffett = run_screen("buffett", rows, figures.get)
    assert [s["symbol"] for s in buffett["stocks"]] == ["A", "B"] and buffett["missing"] == 1
    lynch = run_screen("lynch", rows, figures.get)
    assert [s["symbol"] for s in lynch["stocks"]] == ["B", "A"] and lynch["stocks"][0]["peg"] == 0.8
    graham = run_screen("graham", rows, figures.get)
    assert graham["stocks"] == []                                             # B fails P/E ≤ 15
    magic = run_screen("greenblatt", rows, figures.get)
    assert [s["symbol"] for s in magic["stocks"]] == ["A", "B"] or [s["symbol"] for s in magic["stocks"]] == ["B", "A"]
    assert "C" not in [s["symbol"] for s in magic["stocks"]]                   # financials left out


def test_bad_prints_are_mended_and_splits_adjusted():
    s = pd.Series([100.0, 101, 10.1, 10.2, 103, 104], index=pd.bdate_range("2019-12-16", periods=6))
    fixed, found = repair_splits(s)
    assert fixed.iloc[2] == pytest.approx(101) and found[0][1] == "bad print ×1/10"
    split = pd.Series([1000.0, 1010, 202, 204], index=pd.bdate_range("2020-01-01", periods=4))
    fixed, found = repair_splits(split)
    assert fixed.iloc[0] == pytest.approx(200) and found[0][1] == "split 1:5"
    from desk.data import _clean
    frame = pd.DataFrame({"Open": s, "High": s, "Low": s, "Close": s, "Volume": 1.0})
    assert _clean(frame)["Close"].pct_change().abs().max() < 0.05


def test_a_rebalanced_portfolio_keeps_its_weights():
    idx = pd.bdate_range("2022-01-03", periods=500)
    a = pd.Series(np.linspace(100, 200, 500), index=idx)                       # doubles
    b = pd.Series(100.0, index=idx)                                            # flat
    held = run_portfolio({"A": a, "B": b}, {"A": 50, "B": 50}, rebalance="N", cost_pct=0)
    assert held["metrics"]["final_value"] == pytest.approx(1_500_000, rel=1e-3)
    rebalanced = run_portfolio({"A": a, "B": b}, {"A": 50, "B": 50}, rebalance="Q", cost_pct=0)
    assert 1_400_000 < rebalanced["metrics"]["final_value"] < 1_500_000       # selling the winner each quarter
    cash = run_portfolio({"A": b}, {"A": 50, "CASH": 50}, rebalance="N", cost_pct=0, cash_rate_pct=6.5)
    assert cash["metrics"]["final_value"] > 1_000_000
    with pytest.raises(ValueError, match="add up to 100"):
        run_portfolio({"A": a}, {"A": 60})


# -- API ---------------------------------------------------------------------------------------

@pytest.fixture
def client(tmp_path):
    from fastapi.testclient import TestClient

    from desk.server import create_app

    settings = Settings(tmp_path / "settings.json")
    settings.update({"mcp": {"mcpServers": {"kite": {**FAKE, "env": {"FAKE_KITE_ANCHOR": "2026-11-16"}}}}})

    def fetch(symbol, interval="1d"):
        if symbol == "^INDIAVIX":
            return walk(900, vol=0.0, drift=0.0, base=14.0, start="2023-03-01")
        return walk(900, seed=sum(map(ord, symbol)) % 50, start="2023-03-01", base=24000 if symbol.startswith("^") else 200)

    app = create_app(settings=settings, token="t", fetch=fetch)
    with TestClient(app, headers={"X-Desk-Token": "t"}) as test_client:
        yield test_client
    app.state.kite.disconnect()


def test_api_index_futures_list_chart_and_backtest(client):
    assert client.get("/api/futures").json()["futures"] == []
    client.post("/api/kite/connect")
    client.post("/api/kite/login")
    rows = client.get("/api/futures").json()["futures"]
    nifty = [r for r in rows if r["underlying"] == "NIFTY"]
    assert len(nifty) == 3 and nifty[0]["expiry"] < nifty[1]["expiry"] and nifty[0]["basis"] > 0 and nifty[0]["spot"] == 24500.0
    daily = client.get(f"/api/chart?instrument={nifty[0]['instrument']}&interval=1d").json()
    assert daily["source"] == "Kite" and len(daily["candles"]) > 250
    r = client.post("/api/backtest/futures", json={"underlying": "NIFTY", "start": "2024-01-01", "end": "2026-11-13",
                                                   "strategy": "supertrend", "direction": "both"}).json()
    assert r.get("futures"), r.get("error")
    assert "continuous" in r["data_note"] and r["metrics"]["rolls"] > 10 and r["metrics"]["short_trades"] > 0


def test_api_structures_portfolio_and_screen(client):
    state = client.get("/api/state").json()
    assert {s["key"] for s in state["structures"]} >= {"iron_condor", "short_straddle_hedged", "covered_call"}
    assert {p["key"] for p in state["portfolios"]} >= {"all_weather", "permanent"}
    assert {s["key"] for s in state["screens"]} == {"buffett", "graham", "lynch", "greenblatt", "kedia"}
    r = client.post("/api/backtest/structure", json={"underlying": "NIFTY", "start": "2024-06-01", "end": "2026-10-30",
                                                     "structure": "iron_condor", "mode": "positional", "weekdays": [1]}).json()
    assert r.get("simulated"), r.get("error")
    assert r["metrics"]["trades"] > 20 and "Average margin" in r["data_note"]
    assert "09:15 and 15:30" in client.post("/api/backtest/structure", json={"start": "2026-01-01", "end": "2026-10-30",
                                                                               "structure": "iron_condor", "mode": "intraday",
                                                                               "entry_time": "8am"}).json()["error"]
    p = client.post("/api/backtest/portfolio", json={"portfolio": "all_weather_long"}).json()
    assert p.get("portfolio"), p.get("error")
    assert p["assets"][0]["symbol"] == "NIFTYBEES.NS" and p["buy_hold_name"].startswith("Nifty")
    custom = client.post("/api/backtest/portfolio", json={"weights": {"NIFTYBEES": 50, "CASH": 50}}).json()
    assert custom["strategy"]["name"] == "Custom portfolio"
    assert "add up to 100" in client.post("/api/backtest/portfolio", json={"weights": {"NIFTYBEES": 50}}).json()["error"]
    assert "not loaded yet" in client.post("/api/screens/run", json={"screen": "buffett", "universe": "nifty50"}).json()["error"]


def test_api_passkeys_are_hosted_only_and_guarded(tmp_path):
    from fastapi.testclient import TestClient

    from desk.auth import Auth, hash_password
    from desk.server import create_app

    local = create_app(settings=Settings(tmp_path / "a.json"), token="t")
    with TestClient(local, headers={"X-Desk-Token": "t"}) as c:
        assert c.get("/api/passkeys").json() == {"available": False, "passkeys": []}
    auth = Auth("admin", hash_password("correct horse battery"))
    app = create_app(settings=Settings(tmp_path / "b.json"), auth=auth)
    with TestClient(app, base_url="https://desk.example.com") as c:
        assert c.post("/api/passkey/options").status_code == 404             # none registered yet
        c.post("/api/login", json={"username": "admin", "password": "correct horse battery"})
        csrf = auth._sessions[next(iter(auth._sessions))]["csrf"]
        headers = {"X-Desk-Token": csrf}
        assert "current password" in c.post("/api/passkeys/options", json={"password": "nope"}, headers=headers).json()["error"]
        begun = c.post("/api/passkeys/options", json={"password": "correct horse battery"}, headers=headers).json()
        assert begun["options"]["rp"]["id"] == "desk.example.com"
        assert begun["options"]["authenticatorSelection"]["userVerification"] == "required"
        bad = c.post("/api/passkeys", json={"token": begun["token"], "credential": {"id": "x", "rawId": "x", "type": "public-key",
                                                                                  "response": {}}}, headers=headers).json()
        assert "not accepted" in bad["error"]
        assert "expired" in c.post("/api/passkeys", json={"token": begun["token"], "credential": {}}, headers=headers).json()["error"]
        app.state  # noqa: B018
    with TestClient(app, base_url="http://desk.example.com") as c:
        c.post("/api/login", json={"username": "admin", "password": "correct horse battery"})
        csrf = auth._sessions[list(auth._sessions)[-1]]["csrf"]
        assert "HTTPS" in c.post("/api/passkeys/options", json={"password": "correct horse battery"},
                                 headers={"X-Desk-Token": csrf}).json()["error"]
