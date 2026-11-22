"""Price alerts, the overfitting score and settings heatmap, pair trading and the signal test."""

from datetime import datetime

import numpy as np
import pandas as pd
import pytest

from desk import factortest, luck, market, pairs
from desk.backtest import BacktestParams
from desk.optimiser import optimise
from desk.pricealerts import PriceAlerts
from desk.strategies import STRATEGIES

OPEN = datetime(2026, 11, 20, 11, 0, tzinfo=market.IST)       # a Friday, in the NSE session
SUNDAY = datetime(2026, 11, 22, 11, 0, tzinfo=market.IST)


def frame(close: np.ndarray, end: str = "2026-11-13") -> pd.DataFrame:
    idx = pd.bdate_range(end=end, periods=len(close))
    return pd.DataFrame({"Open": close, "High": close * 1.01, "Low": close * 0.99, "Close": close,
                         "Volume": np.full(len(close), 1e5)}, index=idx)


# -- price alerts ---------------------------------------------------------------------------------

class Feed:
    def __init__(self):
        self.india, self.crypto, self.sent, self.calls = {}, {}, [], 0

    def india_prices(self, instruments):
        self.calls += 1
        return {i: self.india.get(i) for i in instruments}

    def crypto_prices(self, symbols):
        return {s: {"price": self.crypto[s]} for s in symbols if s in self.crypto}

    def notify(self, title, body, level, key):
        self.sent.append((title, body, key))


def alerts(tmp_path, feed, now=OPEN):
    return PriceAlerts(tmp_path / "a.json", feed.notify, feed.india_prices, feed.crypto_prices, now=lambda: now, every=3600)


def test_an_alert_fires_once_when_the_price_reaches_it_and_survives_a_restart(tmp_path):
    feed = Feed()
    book = alerts(tmp_path, feed)
    a = book.add({"market": "india", "condition": "above", "price": 2500, "note": "breakout"}, "NSE:RELIANCE", current=2400)
    feed.india["NSE:RELIANCE"] = 2499.5
    assert book.check() == []
    feed.india["NSE:RELIANCE"] = 2501
    fired = book.check()
    assert [f["id"] for f in fired] == [a["id"]]
    assert "rose to 2,501.00" in feed.sent[0][1] and "breakout" in feed.sent[0][1]
    assert book.check() == [] and len(feed.sent) == 1                 # once only
    again = alerts(tmp_path, feed)
    assert again.list()["alerts"][0]["status"] == "triggered" and again.waiting() == 0
    book.stop()


def test_an_alert_on_the_wrong_side_is_refused_and_bad_input_is_explained(tmp_path):
    book = alerts(tmp_path, Feed())
    with pytest.raises(ValueError, match="already 2,600.00"):
        book.add({"market": "india", "condition": "above", "price": 2500}, "NSE:RELIANCE", current=2600)
    with pytest.raises(ValueError, match="already"):
        book.add({"market": "crypto", "condition": "below", "price": 90000}, "BTCUSDT", current=85000)
    with pytest.raises(ValueError, match="price"):
        book.add({"market": "india", "condition": "above", "price": -1}, "NSE:TCS")
    with pytest.raises(ValueError, match="rise to or fall"):
        book.add({"market": "india", "condition": "cross", "price": 1}, "NSE:TCS")


def test_indian_alerts_wait_for_the_session_but_crypto_is_checked_on_sunday(tmp_path):
    feed = Feed()
    book = alerts(tmp_path, feed, now=SUNDAY)
    book.add({"market": "india", "condition": "below", "price": 100}, "NSE:ITC")
    book.add({"market": "crypto", "condition": "below", "price": 3000}, "ETHUSDT")
    feed.india["NSE:ITC"], feed.crypto["ETHUSDT"] = 50, 2990
    fired = book.check()
    assert [f["instrument"] for f in fired] == ["ETHUSDT"] and feed.calls == 0
    assert "Market closed" in book.list()["status"]["india"]
    book.stop()


def test_a_broker_failure_is_shown_and_does_not_fire_or_crash(tmp_path):
    feed = Feed()

    def broken(_):
        raise RuntimeError("not logged in")

    book = PriceAlerts(tmp_path / "a.json", feed.notify, broken, feed.crypto_prices, now=lambda: OPEN, every=3600)
    book.add({"market": "india", "condition": "below", "price": 100}, "NSE:ITC")
    assert book.check() == [] and "not logged in" in book.list()["status"]["india"]
    book.stop()


# -- overfitting score and heatmap -------------------------------------------------------------------

def test_pure_noise_settings_score_as_luck_and_a_real_edge_does_not():
    scores = [luck.overfitting_score(pd.DataFrame(np.random.default_rng(s).normal(0, 0.01, (1000, 30))))["score_pct"]
              for s in range(8)]
    assert 35 < np.mean(scores) < 65                                # noise: the winner is a coin toss out of sample
    noise = pd.DataFrame(np.random.default_rng(1).normal(0, 0.01, (1000, 30)))
    edge = noise.copy()
    edge[0] += 0.003                                                 # one setting is truly better
    scored = luck.overfitting_score(edge)
    assert scored["score_pct"] < 10 and scored["verdict"] == "low"
    assert luck.overfitting_score(noise[[0]]) is None              # one setting: nothing to compare


def test_heatmap_lays_out_two_settings_and_marks_too_few_trades():
    rows = [{"params": {"fast": f, "slow": s}, "score": (-np.inf if f == 5 and s == 30 else f + s)}
            for f in (5, 10) for s in (30, 60, 90)]
    hm = luck.heatmap(rows, ["fast", "slow"])
    assert hm["xs"] == [5, 10] and hm["ys"] == [30, 60, 90]
    assert hm["cells"][0] == [None, 40] and hm["cells"][2] == [95, 100]
    assert luck.heatmap(rows[:1], ["fast", "slow"]) is None


def test_optimiser_reports_the_luck_score_and_heatmap():
    rng = np.random.default_rng(3)
    bars = frame(100 * np.exp(np.cumsum(rng.normal(0.0003, 0.015, 900))))
    out = optimise(bars, STRATEGIES["sma_cross"], {"fast": [5, 25, 10], "slow": [40, 100, 30]}, BacktestParams(capital=100_000),
                   folds=2, min_trades=1)
    assert out["heatmap"]["x"] == "fast" and len(out["heatmap"]["cells"]) == 3
    assert out["luck"] is None or 0 <= out["luck"]["score_pct"] <= 100


# -- pair trading ---------------------------------------------------------------------------------

def cointegrated(n=900, seed=5):
    rng = np.random.default_rng(seed)
    common = np.cumsum(rng.normal(0, 0.012, n))
    gap = np.zeros(n)
    for i in range(1, n):
        gap[i] = 0.9 * gap[i - 1] + rng.normal(0, 0.01)              # the gap pulls back to zero
    return frame(100 * np.exp(common + gap)), frame(200 * np.exp(common))


def test_a_pair_whose_gap_closes_makes_money_and_trades_both_ways():
    a, b = cointegrated()
    out = pairs.backtest(a, b, lookback=60, entry_z=2, exit_z=0.5, stop_z=4, cost_pct=0.05, names=("A", "B"))
    m = out["metrics"]
    assert m["trades"] >= 10 and m["total_return_pct"] > 0
    assert {t["side"] for t in out["trades"]} == {"Buy A, sell B", "Sell A, buy B"}
    assert out["correlation"] > 0.5 and out["half_life_days"] and out["half_life_days"] < 30
    assert out["now"]


def test_trades_fill_at_the_next_open_and_start_after_the_start_date():
    a, b = cointegrated()
    start = str(a.index[400].date())
    out = pairs.backtest(a, b, start=start)
    assert out["period"][0] == start and all(t["entry_date"] > start for t in out["trades"])
    first = out["trades"][0]
    day = pd.Timestamp(first["entry_date"])
    assert first["entry_a"] == round(float(a.loc[day, "Open"]), 2)


def test_pair_inputs_are_checked():
    a, b = cointegrated(200)
    with pytest.raises(ValueError, match="stoploss gap"):
        pairs.backtest(a, b, entry_z=2, stop_z=1.5)
    with pytest.raises(ValueError, match="days with prices"):
        pairs.backtest(a.iloc[:50], b.iloc[:50])


# -- signal test ----------------------------------------------------------------------------------

def universe(n=60, days=1500, seed=11, edge=True):
    """Stocks whose drift persists: past winners keep winning when ``edge``."""
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(end="2026-11-13", periods=days)
    out = {}
    for k in range(n):
        drift = rng.normal(0, 0.0012) if edge else 0.0
        out[f"S{k}.NS"] = pd.Series(100 * np.exp(np.cumsum(rng.normal(drift, 0.015, days))), index=idx)
    return out


def test_momentum_works_when_winners_keep_winning_and_not_on_noise():
    real = factortest.run(universe(), "mom_6m", start="2023-01-01", end="2026-11-13", hold=21)
    assert real["verdict"] == "works" and real["mean_ic"] > 0.03
    assert real["group_yearly_pct"][-1] > real["group_yearly_pct"][0]
    assert real["top"]["cagr_pct"] > real["all"]["cagr_pct"]
    assert len(real["latest_top"]) == 20 and real["equity"][0][1] == 1.0
    noise = factortest.run(universe(edge=False, seed=0), "mom_6m", start="2023-01-01", end="2026-11-13", hold=21)
    assert noise["verdict"] in ("none", "weak")


def test_signals_never_look_ahead():
    close = pd.DataFrame(universe(25, 600))
    for key in factortest.SIGNALS:
        full = factortest.signal_values(key, close)
        cut = factortest.signal_values(key, close.iloc[:400])
        pd.testing.assert_frame_equal(full.iloc[:400], cut, check_freq=False)


def test_signal_test_inputs_are_checked():
    with pytest.raises(ValueError, match="at least 20"):
        factortest.run(universe(5), "mom_3m", start="2024-01-01", end="2026-11-13")
    with pytest.raises(ValueError, match="signal"):
        factortest.run(universe(), "magic", start="2024-01-01", end="2026-11-13")


# -- through the API ------------------------------------------------------------------------------

@pytest.fixture
def client(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from desk.server import create_app
    from desk.settings import Settings

    monkeypatch.setenv("BHARAT_DESK_HOME", str(tmp_path / "desk"))
    settings = Settings(tmp_path / "settings.json")
    stocks = universe(25, 1500)

    def fetch(symbol, interval="1d"):
        if symbol in stocks:
            return frame(stocks[symbol].to_numpy())
        a, b = cointegrated(1500, seed=sum(map(ord, symbol)) % 7)
        return a if symbol in ("HDFCBANK.NS", "TCS.NS") else b

    app = create_app(settings=settings, token="t", fetch=fetch)
    app.state.indices._state.setdefault("lists", {})["nifty500"] = [{"symbol": s, "name": s[:-3]} for s in stocks]
    app.state.price_alerts.crypto_prices = lambda symbols: {s: {"price": 100.0} for s in symbols}
    with TestClient(app, headers={"X-Desk-Token": "t"}) as test_client:
        yield test_client
    app.state.price_alerts.stop()
    app.state.kite.disconnect()


def test_api_price_alerts_add_list_delete(client):
    listed = client.get("/api/price-alerts").json()
    assert listed["alerts"] == [] and set(listed["markets"]) == {"india", "crypto"}
    refused = client.post("/api/price-alerts", json={"market": "crypto", "symbol": "BTCUSDT", "condition": "above", "price": 50})
    assert "already" in refused.json()["error"]
    added = client.post("/api/price-alerts", json={"market": "crypto", "symbol": "BTC/USDT", "condition": "above", "price": 150}).json()
    assert added["alert"]["instrument"] == "BTCUSDT" and added["alert"]["price_when_set"] == 100.0
    india = client.post("/api/price-alerts", json={"market": "india", "symbol": "RELIANCE", "condition": "below", "price": 1000}).json()
    assert india["alert"]["instrument"] == "NSE:RELIANCE" and india["alert"]["label"] == "RELIANCE"
    assert "error" in client.post("/api/price-alerts", json={"market": "crypto", "symbol": "NOPE", "condition": "above", "price": 1}).json()
    assert client.delete(f"/api/price-alerts/{added['alert']['id']}").status_code == 200
    assert client.delete("/api/price-alerts/0000000000").status_code == 404
    assert [a["instrument"] for a in client.get("/api/price-alerts").json()["alerts"]] == ["NSE:RELIANCE"]


def test_api_pair_backtest_and_options(client):
    options = client.get("/api/pairs/options").json()
    assert options["examples"][0]["a"] == "HDFCBANK" and "mom_12_1" in options["signals"]
    out = client.post("/api/pairs/backtest", json={"a": "HDFCBANK", "b": "ICICIBANK", "start": "2024-01-01",
                                                   "end": "2026-11-13", "capital": 500000}).json()
    assert "error" not in out, out
    assert out["names"] == ["HDFCBANK", "ICICIBANK"] and out["period"][0] >= "2024-01-01" and out["data_note"]
    assert "different" in client.post("/api/pairs/backtest", json={"a": "TCS", "b": "TCS", "start": "2024-01-01",
                                                                    "end": "2026-11-13"}).json()["error"]


def test_api_signal_test_runs_in_the_background(client):
    import time

    started = client.post("/api/signal-test", json={"index": "nifty500", "signal": "mom_6m", "start": "2023-01-01",
                                                    "end": "2026-11-13", "hold": 21}).json()
    job_id = started["job"]["id"]
    for _ in range(300):
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("done", "error"):
            break
        time.sleep(0.1)
    assert job["status"] == "done", job["error"]
    assert job["result"]["stocks"] == 25 and job["result"]["index_name"] and job["progress"] == {"done": 25, "total": 25}
    assert "signal" in client.post("/api/signal-test", json={"index": "nifty500", "signal": "x", "start": "2023-01-01",
                                                              "end": "2026-11-13"}).json()["error"]
