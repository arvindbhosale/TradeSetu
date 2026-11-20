"""Risk-per-trade sizing, the trade journal, your results, and the backtest trust checks."""

import copy
import sys

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("fastapi")
pytest.importorskip("mcp")

import tradingagents.dataflows.config as config_module
from desk import market, results, sizing
from desk.backtest import checks
from desk.pnl import Ledger
from desk.server import create_app
from desk.settings import Settings
from desk.tradenotes import TradeNotes

pytestmark = pytest.mark.unit

FAKE = {"command": sys.executable, "args": ["-m", "desk.dev.fake_kite_mcp"]}
OPEN = {"open": True, "time_ist": "2026-11-16 10:00:00", "note": ""}


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("BHARAT_DESK_HOME", str(tmp_path / "desk"))
    saved = copy.deepcopy(config_module._config)
    yield
    config_module._config = saved


def bars(n=600, seed=3, drift=0.0006):
    rng = np.random.default_rng(seed)
    index = pd.bdate_range("2022-01-03", periods=n)
    close = 1000 * np.exp(np.cumsum(rng.normal(drift, 0.014, n)))
    open_ = np.r_[close[0], close[:-1]] * (1 + rng.normal(0, 0.003, n))
    return pd.DataFrame({"Open": open_, "High": np.maximum(open_, close) * 1.005,
                         "Low": np.minimum(open_, close) * 0.995, "Close": close, "Volume": 1e6}, index=index)


@pytest.fixture
def client(tmp_path):
    from fastapi.testclient import TestClient

    settings = Settings(tmp_path / "settings.json")
    settings.update({"mcp": {"mcpServers": {"kite": FAKE}}, "enforce_trade_window": False})
    app = create_app(settings=settings, token="t0ken", fetch=lambda symbol, interval="1d": bars(seed=sum(map(ord, symbol))))
    with TestClient(app, headers={"X-Desk-Token": "t0ken"}) as test_client:
        yield test_client
    app.state.kite.disconnect()


# -- sizing ------------------------------------------------------------------

def test_size_risks_the_set_share_of_capital_with_charges_inside():
    r = sizing.size({"entry": 1000.0, "stop": 950.0, "risk_pct": 1.0}, 1_000_000)
    # ₹10,000 risk / ₹50 a share = 200, less the shares the round-trip charges push over the budget
    assert 190 <= r["quantity"] < 200 and r["by_risk"] == r["quantity"]
    assert r["risk"] <= 10_000 < sizing.trade_risk("BUY", r["quantity"] + 1, 1000.0, 950.0)
    assert r["stop_loss_pct"] == 5.0 and r["limited_by"] == []


def test_size_is_capped_by_capital_and_max_order_value_and_says_so():
    r = sizing.size({"entry": 100.0, "stop": 99.5, "risk_pct": 2.0}, 100_000, max_order_value=50_000)
    assert r["quantity"] == 500 and len(r["limited_by"]) == 2 and r["by_risk"] > 1000


def test_size_reports_reward_and_the_breakeven_win_rate():
    r = sizing.size({"entry": 1000.0, "stop": 950.0, "target": 1100.0, "risk_pct": 1.0}, 1_000_000)
    assert r["reward_to_risk"] == pytest.approx(r["reward"] / r["risk"], abs=0.01)
    assert 30 < r["breakeven_win_rate_pct"] < 35          # about 1 / (1 + 2)
    assert r["target_pct"] == 10.0


def test_size_rounds_to_lots_and_explains_a_trade_too_big_for_one_lot():
    r = sizing.size({"entry": 200.0, "stop": 150.0, "risk_pct": 1.0, "lot": 75, "exchange": "NFO",
                     "tradingsymbol": "NIFTY25DEC24000CE"}, 100_000)
    assert r["quantity"] == 0 and "lot" in r["note"]
    r = sizing.size({"entry": 200.0, "stop": 190.0, "risk_pct": 1.0, "lot": 75, "exchange": "NFO",
                     "tradingsymbol": "NIFTY25DEC24000CE"}, 1_000_000)
    assert r["quantity"] % 75 == 0 and r["lots"] == r["quantity"] // 75


@pytest.mark.parametrize("raw, message", [
    ({"entry": 100, "stop": 101, "risk_pct": 1}, "below the entry"),
    ({"entry": 100, "risk_pct": 1}, "Stoploss price is needed"),
    ({"entry": 100, "stop": 90, "risk_pct": 11}, "at most 10"),
    ({"entry": 100, "stop": 90, "risk_pct": 1, "target": 95}, "profit side"),
    ({"entry": 100, "stop": 90, "risk_pct": 1, "side": "SELL"}, "above the entry"),
])
def test_size_refuses_nonsense(raw, message):
    with pytest.raises(ValueError, match=message):
        sizing.size(raw, 100_000)


# -- the risk check on a manual order ------------------------------------------

def test_api_preview_refuses_a_manual_buy_whose_stoploss_risks_too_much(client, monkeypatch):
    monkeypatch.setattr(market, "session_status", lambda *a: OPEN)
    price = client.get("/api/quote?symbol=TCS").json()["last_price"]
    # Worth about ₹48,000 (under the ₹50,000 order limit) with a stoploss at half the price: over 2% of ₹10 lakh.
    ticket = {"exchange": "NSE", "tradingsymbol": "TCS", "side": "BUY", "quantity": int(48_000 // price),
              "order_type": "MARKET"}
    stop = round(price * 0.5 / 0.05) * 0.05
    review = client.post("/api/order/preview", json={"mode": "paper", "order": ticket, "stop": stop}).json()
    assert not review["ok"] and any("limit is 2%" in e for e in review["errors"])
    assert review["risk"]["of_capital_pct"] > 2
    ok = client.post("/api/order/preview", json={"mode": "paper", "order": {**ticket, "quantity": 1}, "stop": stop}).json()
    assert ok["ok"] and ok["risk"]["loss"] > 0
    wrong = client.post("/api/order/preview", json={"mode": "paper", "order": {**ticket, "quantity": 1},
                                                    "stop": round(price * 1.2, 0)}).json()
    assert any("below the entry" in e for e in wrong["errors"])
    client.put("/api/settings", json={"require_stoploss": True})
    bare = client.post("/api/order/preview", json={"mode": "paper", "order": {**ticket, "quantity": 1}}).json()
    assert any("stoploss on every buy" in e for e in bare["errors"])
    sell = client.post("/api/order/preview", json={"mode": "paper", "order": {**ticket, "side": "SELL", "quantity": 1}}).json()
    assert not any("stoploss" in e for e in sell["errors"])        # an exit is never held up by the risk rules


def test_api_order_size_uses_the_settings(client):
    client.put("/api/settings", json={"risk_capital": 200_000, "risk_per_trade_pct": 0.5})
    r = client.post("/api/order/size", json={"entry": 500.0, "stop": 490.0}).json()
    assert r["capital"] == 200_000 and r["risk_pct"] == 0.5 and r["risk"] <= 1000
    assert "must be above" in client.put("/api/settings", json={"risk_per_trade_pct": 0}).json()["error"]
    assert "cannot exceed 10" in client.put("/api/settings", json={"max_risk_pct": 25}).json()["error"]


# -- the trade journal -----------------------------------------------------------------

def _fills(ledger: Ledger):
    ledger.add("paper", "manual", "NSE", "TCS", "BUY", 10, 1000.0, charges=0, time_ist="2026-11-02 10:00:00")
    ledger.add("paper", "manual", "NSE", "TCS", "SELL", 5, 1100.0, charges=0, time_ist="2026-11-03 10:00:00")
    ledger.add("paper", "manual", "NSE", "TCS", "SELL", 5, 1050.0, charges=0, time_ist="2026-11-04 10:00:00")
    ledger.add("paper", "manual", "NSE", "TCS", "BUY", 4, 1000.0, charges=0, time_ist="2026-11-05 10:00:00")


def test_partial_exits_add_up_to_one_trade_and_a_plan_ties_to_its_fill(tmp_path):
    ledger = Ledger(tmp_path / "fills.jsonl")
    _fills(ledger)
    notes = TradeNotes(tmp_path / "notes.json")
    notes.add({"mode": "paper", "exchange": "NSE", "symbol": "TCS", "setup": "Breakout", "stop": 950})
    with open(notes.path) as handle:                        # written a few seconds after the buy filled
        data = __import__("json").load(handle)
    next(iter(data.values()))["created"] = "2026-11-02 10:00:05"
    notes.path.write_text(__import__("json").dumps(data))
    closed, open_rows = ledger.closed_trades("paper"), ledger.report("paper")["open"]
    trades = [{"key": r["key"], "mode": "paper", "source": r["source"], "exchange": r["exchange"], "symbol": r["symbol"],
               "opened_at": r["opened_at"]} for r in [*closed, *open_rows]]
    linked = notes.attach(trades)
    rows = results.positions(closed, open_rows, linked)
    assert [r["status"] for r in rows] == ["Open", "Closed"]
    done = rows[1]
    assert done["pnl"] == 750 and done["quantity"] == 10 and done["exit"] == 1075 and done["days"] == 2
    assert done["note"]["setup"] == "Breakout" and done["r"] == 1.5        # ₹750 / (₹50 x 10)
    assert rows[0]["note"] is None                                         # the plan stays with the first trade
    assert notes.pending("paper") == []


def test_notes_validate_and_refuse_a_second_note_for_one_trade(tmp_path):
    notes = TradeNotes(tmp_path / "notes.json")
    key = "paper|manual|NSE|TCS|2026-11-02 10:00:00"
    note = notes.add({"mode": "paper", "exchange": "NSE", "symbol": "tcs", "key": key, "mistakes": ["Exited early"]})
    assert note["symbol"] == "TCS" and note["source"] == "manual"
    with pytest.raises(ValueError, match="already has a note"):
        notes.add({"mode": "paper", "exchange": "NSE", "symbol": "TCS", "key": key})
    with pytest.raises(ValueError, match="mistakes"):
        notes.update(note["id"], {"mistakes": ["Bad luck"]})
    with pytest.raises(ValueError, match="other account"):
        notes.add({"mode": "live", "exchange": "NSE", "symbol": "TCS", "key": key})
    updated = notes.update(note["id"], {"followed_plan": "no", "lesson": "Wait for the close", "symbol": "INFY"})
    assert updated["symbol"] == "TCS" and updated["followed_plan"] == "no"
    notes.delete(note["id"])
    with pytest.raises(KeyError):
        notes.delete(note["id"])


# -- your results ------------------------------------------------------------------------

def _trade(pnl, day, setup=None, mistakes=(), r=None):
    return {"pnl": pnl, "closed_at": f"2026-10-{day:02d} 15:00:00", "opened_at": f"2026-10-{day:02d} 09:45:00",
            "days": 0, "side": "LONG", "source": "manual", "status": "Closed", "r": r,
            "note": {"setup": setup, "mistakes": list(mistakes), "followed_plan": "yes"} if setup else None}


def test_summary_statistics_are_right():
    trades = [_trade(p, d + 1) for d, p in enumerate([100, -50, -50, -50, 200, -50])]
    s = results.summarize(trades)
    assert s["trades"] == 6 and s["win_rate"] == 33.3 and s["expectancy"] == 16.67
    assert s["avg_win"] == 150 and s["avg_loss"] == -50 and s["payoff"] == 3 and s["profit_factor"] == 1.5
    assert s["losing_streak"] == 3 and s["max_drawdown"] == -150 and not s["enough"]
    assert s["edge"] == "unclear" and s["range95"][0] < 0 < s["range95"][1]


def test_a_steady_edge_reads_as_positive_and_groups_split_by_setup_and_mistake():
    trades = [_trade(100 + (i % 3), i % 28 + 1, setup="Breakout", r=1.0) for i in range(40)]
    trades += [_trade(-80, 5, setup="Gap and go", mistakes=["Entered late (chased)"])]
    report = results.report(trades)
    assert report["overall"]["edge"] == "positive" and report["overall"]["enough"]
    by_setup = {r["name"]: r for r in report["by"]["setup"]}
    assert by_setup["Breakout"]["trades"] == 40 and by_setup["Gap and go"]["expectancy"] == -80
    assert {r["name"] for r in report["by"]["mistake"]} == {"No mistake ticked", "Entered late (chased)"}
    assert report["by"]["hour"][0]["name"] == "09:00 to 10:00"
    assert report["overall"]["expectancy_r"] == 1.0 and report["overall"]["r_trades"] == 40


def test_api_journal_flow(client, monkeypatch):
    monkeypatch.setattr(market, "session_status", lambda *a: OPEN)
    ticket = {"exchange": "NSE", "tradingsymbol": "TCS", "side": "BUY", "quantity": 2, "order_type": "MARKET"}
    review = client.post("/api/order/preview", json={"mode": "paper", "order": ticket}).json()
    client.post("/api/order/place", json={"token": review["token"], "phrase": "BUY 2 TCS"})
    plan = client.post("/api/journal", json={"mode": "paper", "exchange": "NSE", "symbol": "TCS", "setup": "Breakout",
                                             "reason": "Closed above the 20-day high", "stop": 1.0}).json()
    sell = client.post("/api/order/preview", json={"mode": "paper", "order": {**ticket, "side": "SELL"}}).json()
    client.post("/api/order/place", json={"token": sell["token"], "phrase": "SELL 2 TCS"})
    j = client.get("/api/journal?mode=paper").json()
    assert len(j["trades"]) == 1 and j["trades"][0]["note"]["id"] == plan["id"] and j["pending"] == []
    assert j["results"]["overall"]["trades"] == 1 and j["other"]["mode"] == "live" and "Breakout" in j["setups"]
    note = client.put(f"/api/journal/{plan['id']}", json={"followed_plan": "yes", "mistakes": ["Exited early"]}).json()
    assert note["mistakes"] == ["Exited early"]
    assert client.put("/api/journal/nope", json={}).status_code == 404
    assert client.delete(f"/api/journal/{plan['id']}").json()["ok"]
    assert "mode must be" in client.get("/api/journal?mode=both").json()["error"]


# -- backtest checks -----------------------------------------------------------------------

def test_checks_flag_few_trades_and_compare_with_a_safe_rate():
    c = checks([1000.0, -500.0, 800.0], 100_000, 101_300, 2.0, 6.5)
    assert c["few_trades"] and not c["beats_safe"] and c["safe_final"] == pytest.approx(100_000 * 1.065 ** 2, abs=0.01)
    assert "monte_carlo" not in c                                   # too few to shuffle


def test_monte_carlo_is_repeatable_and_brackets_the_result():
    rng = np.random.default_rng(1)
    pnls = list(rng.normal(300, 2000, 80))
    a, b = checks(pnls, 100_000, 100_000 + sum(pnls), 3.0, 6.5), checks(pnls, 100_000, 100_000 + sum(pnls), 3.0, 6.5)
    assert a == b
    mc = a["monte_carlo"]
    assert mc["final"]["low"] < 100_000 + sum(pnls) < mc["final"]["high"]
    assert mc["drawdown_pct"]["bad"] <= mc["drawdown_pct"]["median"] <= 0
    assert 0 <= mc["loss_chance_pct"] <= 100


def test_backtest_results_carry_the_checks(client):
    body = {"symbol": "TCS", "start": "2022-03-01", "end": "2024-03-01", "strategy": "sma_cross",
            "params": {"fast": 10, "slow": 30}}
    result = client.post("/api/backtest/strategy", json=body).json()
    c = result["metrics"]["checks"]
    assert c["trades"] == result["metrics"]["trades"] and c["safe_rate_pct"] == 6.5
