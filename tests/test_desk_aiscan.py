"""AI strategy scan: the bounded search, the luck test, the verdicts, the workbook and the API."""

import io
import sys
import time

import numpy as np
import pandas as pd
import pytest

from desk import aiscan
from desk.backtest import BacktestParams
from desk.strategies import STRATEGIES

END = "2026-11-13"


def trending(seed: int, n: int = 2600, phi: float = 0.25) -> pd.DataFrame:
    """Prices whose daily moves carry on (autocorrelated): a real edge for trend and breakout rules."""
    rng = np.random.default_rng(seed)
    shocks = rng.normal(0, 0.015, n)
    moves = np.zeros(n)
    for i in range(1, n):
        moves[i] = phi * moves[i - 1] + shocks[i]
    close = 100 * np.exp(np.cumsum(moves))
    open_ = np.r_[close[0], close[:-1]] * np.exp(rng.normal(0, 0.002, n))
    return pd.DataFrame({"Open": open_, "High": np.maximum(open_, close) * 1.005, "Low": np.minimum(open_, close) * 0.995,
                         "Close": close, "Volume": rng.integers(100_000, 1_000_000, n).astype(float)},
                        index=pd.bdate_range(end=END, periods=n))


def random_walk(seed: int, n: int = 2600) -> pd.DataFrame:
    return trending(seed, n, phi=0.0)


def stocks(n: int) -> list[dict]:
    return [{"symbol": f"S{i}.NS", "name": f"Stock {i}"} for i in range(n)]


def test_the_search_is_bounded_and_neighbours_are_one_step_apart():
    quick = aiscan.candidates("quick")
    keys = aiscan.strategy_keys()
    assert len(quick) == len(keys) and keys[-1] == "buy_hold"
    assert not any(STRATEGIES[k].intraday_only for k in keys)
    standard = aiscan.candidates("standard")
    per = {}
    for c in standard:
        per.setdefault(c["strategy"], []).append(c)
        STRATEGIES[c["strategy"]].resolve(c["params"])            # every value is inside the strategy's limits
    assert max(len(v) for v in per.values()) <= 9
    sma = per["sma_cross"]
    assert sorted({c["params"]["fast"] for c in sma}) == [12, 20, 30] and sorted({c["params"]["slow"] for c in sma}) == [30, 50, 75]
    assert all(set(c["axes"]) == {"fast", "slow"} for c in sma)
    deep = {}
    for c in aiscan.candidates("deep"):
        deep[c["strategy"]] = deep.get(c["strategy"], 0) + 1
    assert max(deep.values()) <= 27
    with pytest.raises(ValueError, match="depth"):
        aiscan.candidates("everything")


def test_the_luck_test_gets_stricter_with_more_trials_and_rewards_a_real_edge():
    rng = np.random.default_rng(3)
    edge = rng.normal(0.0012, 0.01, 750)          # Sharpe about 1.9 a year over three years
    one, many = aiscan.deflated_sharpe(edge, 6.5, 1), aiscan.deflated_sharpe(edge, 6.5, 200)
    assert one > many > 0.5 and one > 0.99
    noise = rng.normal(0.0002, 0.01, 750)
    assert aiscan.deflated_sharpe(noise, 6.5, 30) < 0.5
    assert aiscan.deflated_sharpe(np.zeros(100), 6.5, 30) is None


def test_segment_stats_reads_an_equity_curve():
    days = pd.bdate_range("2024-01-01", periods=253)
    values = np.linspace(100, 110, 253)
    s = aiscan.segment_stats(values, days, 0.0)
    assert s["return_pct"] == 10.0 and s["max_drawdown_pct"] == 0.0 and s["sharpe"] > 5


def test_a_real_edge_wins_and_is_judged_on_unseen_data_only():
    result = aiscan.scan(stocks(6), lambda s: (trending(int(s[1:-3])), "Test"), BacktestParams(capital=600_000),
                         start="2016-11-01", end=END, depth="quick")
    w = result["winner"]
    assert w and w["verdict"] in ("Beats buy and hold", "Works") and w["luck_test"] >= aiscan.DSR_PASS
    assert w["unseen"]["return_pct"] > 0 and w["oos_trades"] >= 30
    assert result["period"]["unseen"][0] >= result["cut"]
    assert result["period"]["in_sample"][1] < result["cut"]
    ranks = [b["rank"] for b in result["ranking"]]
    assert ranks == list(range(1, len(ranks) + 1)) and all(b["strategy"] != "buy_hold" for b in result["ranking"])
    # Stoploss and target were tried on three strategies, five levels each, plus each one's own exit.
    assert len(result["exit_trials"]) == 3 * 6 and sum(r["chosen"] == "Yes" for r in result["exit_trials"]) == 3
    assert result["luck_trials"] == result["strategies_compared"] + 15
    assert len(result["per_stock"]) == 6 and result["stocks_tested"] == 6
    assert len(result["runs"]) == result["trials"] == 6 * (len(aiscan.candidates("quick")) + 15)
    assert all(r["charges"] > 0 for r in result["runs"] if r["trades"])        # every trade pays charges
    assert "Winner:" in result["explanation"] and "survivorship" in result["explanation"]
    assert len(result["equity"]["days"]) == len(result["equity"]["winner"]) == len(result["equity"]["buy_hold"])


def test_noise_finds_no_winner():
    result = aiscan.scan(stocks(5), lambda s: (random_walk(int(s[1:-3]) + 100), "Test"), BacktestParams(capital=500_000),
                         start="2016-11-01", end=END, depth="quick")
    assert result["winner"] is None
    assert all(b["verdict"] not in ("Beats buy and hold", "Works") for b in result["ranking"])
    assert "No strategy passed" in result["explanation"]


def test_stocks_without_prices_or_money_are_left_out_and_a_stop_keeps_what_was_tested():
    def load(symbol):
        if symbol == "S0.NS":
            raise RuntimeError("Kite returned nothing")
        if symbol == "S1.NS":
            return trending(1, n=100), "Test"
        if symbol == "S2.NS":
            return trending(2) * 100000, "Test"               # one share costs more than the money per stock
        return trending(int(symbol[1:-3])), "Test"

    calls = []
    result = aiscan.scan(stocks(6), load, BacktestParams(capital=600_000), start="2016-11-01", end=END, depth="quick",
                         stopped=lambda: len(calls) >= 5, progress=lambda d, t: calls.append(d))
    reasons = {s["symbol"]: s["reason"] for s in result["skipped"]}
    assert "Kite returned nothing" in reasons["S0.NS"] and "day candles" in reasons["S1.NS"] and "costs more" in reasons["S2.NS"]
    assert result["stopped"] and result["stocks_tested"] == 2 and result["exit_trials"] == []
    assert "stopped early" in result["explanation"]


def test_the_scan_refuses_bad_inputs():
    load = lambda s: (trending(1), "Test")  # noqa: E731
    with pytest.raises(ValueError, match="at least ₹"):
        aiscan.scan(stocks(10), load, BacktestParams(capital=50_000), start="2016-11-01", end=END)
    with pytest.raises(ValueError, match="at least a year"):
        aiscan.scan(stocks(2), load, BacktestParams(capital=100_000), start="2026-06-01", end=END)
    with pytest.raises(ValueError, match="unseen"):
        aiscan.scan(stocks(2), load, BacktestParams(capital=100_000), start="2016-11-01", end=END, oos_pct=80)


def test_worker_processes_give_the_same_answer(tmp_path):
    if sys.platform == "win32":
        pytest.skip("spawned workers re-import the test module")
    load = lambda s: (trending(int(s[1:-3])), "Test")  # noqa: E731
    one = aiscan.scan(stocks(3), load, BacktestParams(capital=300_000), start="2016-11-01", end=END, depth="quick")
    two = aiscan.scan(stocks(3), load, BacktestParams(capital=300_000), start="2016-11-01", end=END, depth="quick", workers=2)
    assert [(b["name"], b["unseen"]) for b in one["ranking"]] == [(b["name"], b["unseen"]) for b in two["ranking"]]


def test_the_price_cache_keeps_a_finished_range(tmp_path):
    cache = aiscan.PriceCache(tmp_path / "c")
    assert cache.get("TCS.NS", "2020-01-01", "2024-01-01") is None
    bars = trending(1, n=300)
    cache.put("TCS.NS", "2020-01-01", "2024-01-01", bars, "Kite")
    frame, source = cache.get("TCS.NS", "2020-01-01", "2024-01-01")
    assert source == "Kite" and len(frame) == 300 and abs(frame["Close"].iloc[-1] - bars["Close"].iloc[-1]) < 1e-6


def test_the_workbook_has_every_sheet_with_colours():
    from openpyxl import load_workbook

    from desk.xlsxexport import scan_workbook
    result = aiscan.scan(stocks(3), lambda s: (trending(int(s[1:-3])), "Test"), BacktestParams(capital=300_000),
                         start="2016-11-01", end=END, depth="quick", index_name="Nifty Test")
    wb = load_workbook(io.BytesIO(scan_workbook(result, "Desk")))
    assert wb.sheetnames == ["Summary", "Read me", "Best per stock", "Stoploss and target", "All runs", "Notes"]
    summary = wb["Summary"]
    assert summary["A1"].value == "AI strategy scan: Nifty Test" and summary["B4"].value == "Verdict"
    assert summary["C5"].value == result["ranking"][0]["name"]
    assert len(summary.conditional_formatting) >= 4
    runs = wb["All runs"]
    assert runs.max_row >= 4 + len(result["runs"]) and runs.auto_filter.ref.startswith("A4")
    assert "Deflated Sharpe" in " ".join(str(c.value) for row in wb["Read me"].iter_rows() for c in row if c.value)


# -- through the API ------------------------------------------------------------------------------------

@pytest.fixture
def client(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    pytest.importorskip("mcp")
    from fastapi.testclient import TestClient

    from desk.server import create_app
    from desk.settings import Settings

    monkeypatch.setenv("BHARAT_DESK_HOME", str(tmp_path / "desk"))
    monkeypatch.setenv("DESK_SCAN_WORKERS", "1")
    settings = Settings(tmp_path / "settings.json")
    fetch = lambda symbol, interval="1d": trending(sum(map(ord, symbol)) % 40, n=1300) * (20 if symbol.startswith("^") else 1)  # noqa: E731
    app = create_app(settings=settings, token="t", fetch=fetch)
    app.state.indices._state.setdefault("lists", {})["nifty50"] = [
        {"symbol": f"{name}.NS", "name": name, "industry": "IT"} for name in ("TCS", "INFY", "WIPRO")]
    with TestClient(app, headers={"X-Desk-Token": "t"}) as test_client:
        yield test_client
    app.state.kite.disconnect()


def test_api_runs_a_scan_in_the_background_and_serves_the_workbook(client):
    options = client.get("/api/scan/options").json()
    nifty = next(x for x in options["indices"] if x["id"] == "nifty50")
    assert nifty["stocks"] == 3 and set(options["depths"]) == {"quick", "standard", "deep"}
    assert client.post("/api/scan", json={"index": "nifty50", "start": "2026-01-01", "end": END}).json()["error"].startswith("Use at least a year")
    started = client.post("/api/scan", json={"index": "nifty50", "start": "2022-01-01", "end": END, "depth": "quick",
                                             "capital": 300_000}).json()
    job_id = started["job"]["id"]
    for _ in range(600):
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("done", "error"):
            break
        time.sleep(0.1)
    assert job["status"] == "done", job["error"]
    result = job["result"]
    assert "runs" not in result and result["stocks_tested"] == 3 and result["index"] == "Nifty 50"
    assert job["progress"] == {"done": 3, "total": 3}
    book = client.get(result["workbook"])
    assert book.status_code == 200 and book.headers["content-type"].startswith("application/vnd.openxmlformats")
    assert client.get("/api/scan/zzzz.xlsx").status_code == 404
    assert client.post(f"/api/jobs/{job_id}/stop").json()["status"] == "done"
    # The second scan of the same dates reads the saved prices.
    again = client.post("/api/scan", json={"index": "nifty50", "start": "2022-01-01", "end": END, "depth": "quick",
                                           "capital": 300_000}).json()["job"]["id"]
    for _ in range(600):
        job = client.get(f"/api/jobs/{again}").json()
        if job["status"] in ("done", "error"):
            break
        time.sleep(0.1)
    assert job["status"] == "done" and any("saved copy" in e["message"] for e in job["events"])
