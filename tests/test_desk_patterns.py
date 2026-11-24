"""Candle patterns: shape rules, no look-ahead, measured odds against the normal candle, the range of likely
prices, the strategy rule, the pooled count through the API, and the browser's copy matching the desk's."""

import json
import shutil
import subprocess
import time
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from desk import candles, custom
from desk.chartstore import clean_layout

ROOT = Path(__file__).resolve().parents[1]


def walk(n=1500, seed=1, drift=0.0003, vol=0.015, end="2026-11-13") -> pd.DataFrame:
    """Random OHLCV candles: a random walk has no pattern edge by construction."""
    rng = np.random.default_rng(seed)
    c = 100 * np.exp(np.cumsum(rng.normal(drift, vol, n)))
    o = np.r_[c[0], c[:-1]] * np.exp(rng.normal(0, vol / 3, n))
    h = np.maximum(o, c) * np.exp(np.abs(rng.normal(0, vol / 2, n)))
    lo = np.minimum(o, c) * np.exp(-np.abs(rng.normal(0, vol / 2, n)))
    v = rng.integers(1000, 5000, n).astype(float)
    return pd.DataFrame({"Open": o, "High": h, "Low": lo, "Close": c, "Volume": v}, index=pd.bdate_range(end=end, periods=n))


def scripted(rows: list[tuple[float, float, float, float]]) -> pd.DataFrame:
    """Twelve quiet candles drifting down from 110, then ``rows`` (open, high, low, close)."""
    base = [(110 - i, 110.6 - i, 109.0 - i, 109.4 - i) for i in range(12)]
    data = base + rows
    return pd.DataFrame(data, columns=["Open", "High", "Low", "Close"],
                        index=pd.bdate_range("2026-01-01", periods=len(data))).assign(Volume=1000.0)


def last(bars):
    found = candles.detect(bars).iloc[-1]
    return {k for k, v in found.items() if v}


# -- shapes ------------------------------------------------------------------------------------------

def test_textbook_shapes_are_found():
    assert "hammer" in last(scripted([(98.0, 98.3, 95.0, 98.2)]))            # after a fall, long lower wick
    assert "doji" in last(scripted([(98.0, 99.0, 97.0, 98.05)]))
    engulf = scripted([(98.4, 98.6, 97.0, 97.2), (97.0, 99.4, 96.9, 99.0)])  # red, then a bigger green around it
    assert "bullish_engulfing" in last(engulf)
    star = scripted([(98.4, 98.5, 95.4, 95.6), (95.4, 95.7, 94.9, 95.3), (95.5, 97.6, 95.4, 97.5)])
    assert "morning_star" in last(star)
    assert "hanging_man" not in last(scripted([(98.0, 98.3, 95.0, 98.2)]))   # same shape, but it fell before


def test_patterns_never_look_ahead():
    bars = walk(400)
    full = candles.detect(bars)
    for cut in (120, 233, 399):
        assert candles.detect(bars.iloc[:cut]).equals(full.iloc[:cut])
    ctx_full, ctx_cut = candles.context(bars), candles.context(bars.iloc[:250])
    assert ctx_cut.equals(ctx_full.iloc[:250])


def test_the_forming_candle_is_left_out():
    bars = walk(600)
    a = candles.odds(bars, closed=599)
    b = candles.odds(bars.iloc[:599])
    assert a == b


# -- odds -----------------------------------------------------------------------------------------------

def test_odds_compare_with_the_normal_candle_and_flag_few_cases():
    out = candles.odds(walk(1500))
    assert out["tested"] == len(candles.PATTERNS) * 3 and out["normal"]["5"]["cases"] == 1495
    by = {p["key"]: p for p in out["patterns"]}
    assert by["morning_star"]["after"]["5"]["verdict"]["key"] == "few"
    doji = by["doji"]["after"]["5"]
    assert doji["cases"] >= 30 and doji["verdict"]["z"] is not None
    # a random walk: nothing may be called a clear difference
    assert not [p["key"] for p in out["patterns"] for hz in ("1", "5", "10") if p["after"][hz]["verdict"]["key"] in ("up", "down")]


def test_a_real_edge_is_found():
    """Plant an edge: the candle after every Bullish Engulfing opens 1.5% higher (everything after moves up too)."""
    bars = walk(2500, seed=4)
    found = candles.detect(bars)["bullish_engulfing"].to_numpy()
    scale = np.ones(len(bars))
    for i in np.flatnonzero(found):
        scale[i + 1:] *= 1.015
    bars = bars.assign(**{k: bars[k] * scale for k in ("Open", "High", "Low", "Close")})
    out = {p["key"]: p for p in candles.odds(bars)["patterns"]}
    assert out["bullish_engulfing"]["after"]["1"]["cases"] > 100
    assert out["bullish_engulfing"]["after"]["1"]["verdict"]["key"] == "up"


def test_verdict_words():
    assert candles.verdict(10, 90, 50)["key"] == "few"
    assert candles.verdict(100, 66, 50)["key"] == "up"            # z = 3.2
    assert candles.verdict(100, 61, 50)["key"] == "maybe_up"      # z = 2.2
    assert candles.verdict(100, 52, 50)["key"] == "none"
    assert candles.verdict(100, 35, 50)["key"] == "down"


def test_filters_are_checked():
    assert candles.clean_filters({"trend": "up", "place": "low", "volume": 1})["volume"] is True
    for bad in ({"trend": "sideways"}, {"place": "middle"}, {"near": 9}):
        with pytest.raises(ValueError):
            candles.clean_filters(bad)
    narrow = candles.odds(walk(1500), filters={"trend": "up", "volume": True})
    assert narrow["normal"]["5"]["cases"] < 1495


# -- range of likely prices -----------------------------------------------------------------------------

def test_range_widens_and_holds_about_eight_in_ten():
    out = candles.cone(walk(1500), 10)
    widths = [s["q90"] - s["q10"] for s in out["steps"]]
    assert widths == sorted(widths) and all(s["q10"] < s["q25"] < s["q50"] < s["q75"] < s["q90"] for s in out["steps"])
    assert 70 <= out["held"]["pct"] <= 90 and out["held"]["checks"] > 300
    assert candles.cone(walk(60), 10) is None


# -- strategy rule and saved layout ---------------------------------------------------------------------

def test_candle_pattern_rule_in_a_custom_strategy():
    spec = custom.validate({"name": "Hammer buy", "entry": {"match": "all", "rules": [
        {"left": {"indicator": "pattern"}, "op": "is_one_of", "right": {"patterns": ["morning_star", "hammer"]}}]},
        "exit": {"match": "any", "rules": [{"left": {"indicator": "close"}, "op": "<", "right": {"indicator": "sma", "params": {"period": 20}}}]}})
    assert spec["entry"]["rules"][0]["right"] == {"patterns": ["hammer", "morning_star"]}
    strategy = custom.as_strategy("x", spec)
    assert "Candle pattern is one of Hammer, Morning Star" in strategy.describe()["description"]
    bars = walk(800)
    entries = custom._passes(spec["entry"], bars, {})
    expect = candles.detect(bars)[["hammer", "morning_star"]].any(axis=1)
    assert entries.equals(expect)
    for right, op in (({"patterns": []}, "is_one_of"), ({"patterns": ["nope"]}, "is_one_of"), ({"value": 1}, ">")):
        with pytest.raises(ValueError):
            custom.validate({"name": "x", "entry": {"match": "all", "rules": [{"left": {"indicator": "pattern"}, "op": op, "right": right}]},
                             "exit": {"match": "any", "rules": [{"left": {"indicator": "close"}, "op": ">", "right": {"value": 1}}]}})


def test_layout_keeps_pattern_settings():
    out = clean_layout({"venue": "india", "symbol": "TCS", "tf": "1d", "indicators": [],
                        "patterns": {"on": True, "ahead": 5, "trend": "up", "near": 1.5, "hide": ["doji"]}})
    assert out["patterns"] == {"on": True, "ahead": 5, "trend": "up", "near": 1.5, "hide": ["doji"]}
    with pytest.raises(ValueError):
        clean_layout({"patterns": {"hide": "doji"}})


# -- pooled across an index, through the API ------------------------------------------------------------

@pytest.fixture
def client(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from desk.server import create_app
    from desk.settings import Settings

    monkeypatch.setenv("BHARAT_DESK_HOME", str(tmp_path / "desk"))
    stocks = [f"S{k}.NS" for k in range(6)]

    def fetch(symbol, interval="1d"):
        return walk(1600, seed=sum(map(ord, symbol)))

    app = create_app(settings=Settings(tmp_path / "settings.json"), token="t", fetch=fetch)
    app.state.indices._state.setdefault("lists", {})["nifty50"] = [{"symbol": s, "name": s[:-3]} for s in stocks]
    with TestClient(app, headers={"X-Desk-Token": "t"}) as test_client:
        yield test_client
    app.state.price_alerts.stop()
    app.state.kite.disconnect()


def test_api_pooled_odds_and_state(client):
    state = client.get("/api/state").json()
    assert {p["key"] for p in state["candle_patterns"]} == set(candles.PATTERNS)
    assert any(o["key"] == "pattern" and o["kind"] == "pattern" for o in state["indicators"])
    started = client.post("/api/patterns/pooled", json={"index": "nifty50", "filters": {"trend": "down"}, "end": "2026-06-30"}).json()
    job_id = started["job"]["id"]
    for _ in range(300):
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("done", "error"):
            break
        time.sleep(0.1)
    assert job["status"] == "done", job.get("error")
    r = job["result"]
    assert r["stocks"] == 6 and r["end"] == "2026-06-30" and r["filters"]["trend"] == "down" and "today's" in r["note"]
    assert r["normal"]["5"]["cases"] > 1000
    assert "error" in client.post("/api/patterns/pooled", json={"index": "nifty50", "min_cases": 2}).json()
    assert "error" in client.post("/api/patterns/pooled", json={"index": "nifty50", "filters": {"trend": "x"}}).json()


# -- the browser's copy ---------------------------------------------------------------------------------

@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_js_patterns_match_the_python_ones():
    bars = walk(900, seed=7, end="2026-11-13")
    # Make some rarer shapes common enough to compare: every 7th candle a small body at the top of a long wick.
    o, c = bars["Open"].to_numpy().copy(), bars["Close"].to_numpy().copy()
    hi, lo = bars["High"].to_numpy().copy(), bars["Low"].to_numpy().copy()
    for i in range(20, 900, 7):
        c[i] = o[i] * 1.001
        hi[i] = c[i] * 1.0005
        lo[i] = o[i] * 0.98
    bars = bars.assign(Open=o, High=hi, Low=lo, Close=c)
    rows = [{"time": int(t.value // 10**9), "open": r.Open, "high": r.High, "low": r.Low, "close": r.Close, "volume": r.Volume}
            for t, r in zip(bars.index, bars.itertuples(), strict=True)]
    module = (ROOT / "desk/static/chartdesk/patterns.js").as_uri()
    script = """
      import * as p from "MODULE";
      import { readFileSync } from "node:fs";
      const bars = JSON.parse(readFileSync(0, "utf8"));
      const found = p.detect(bars);
      const ctx = p.context(bars, 1);
      console.log(JSON.stringify({ found, ctx, odds: p.odds(bars, { filters: { trend: "down", place: "low" } }),
        plain: p.odds(bars), cone: p.cone(bars, 10), closed: p.odds(bars, { closed: 850 }) }));
    """.replace("MODULE", module)
    done = subprocess.run(["node", "--input-type=module", "-e", script], input=json.dumps(rows), capture_output=True, text=True, check=True)
    js = json.loads(done.stdout)

    py = candles.detect(bars)
    expect = [[k for k in candles.PATTERNS if py[k].iloc[i]] for i in range(len(bars))]
    assert js["found"] == expect
    assert sum(map(len, expect)) > 150 and py["hammer"].sum() > 5

    ctx = candles.context(bars, 1.0)
    assert js["ctx"]["trend"] == ctx["trend"].tolist()
    for a, b in (("nearLow", "near_low"), ("nearHigh", "near_high"), ("highVolume", "high_volume")):
        assert js["ctx"][a] == ctx[b].tolist(), a

    def same(js_odds, py_odds):
        assert js_odds["normal"].keys() == py_odds["normal"].keys()
        for hz in py_odds["normal"]:
            assert js_odds["normal"][hz]["cases"] == py_odds["normal"][hz]["cases"]
            assert js_odds["normal"][hz]["avg_pct"] == pytest.approx(py_odds["normal"][hz]["avg_pct"], abs=0.011)
        for a, b in zip(js_odds["patterns"], py_odds["patterns"], strict=True):
            assert a["key"] == b["key"] and a["seen"] == b["seen"]
            for hz, s in b["after"].items():
                t = a["after"][hz]
                assert t["cases"] == s["cases"] and t["verdict"]["key"] == s["verdict"]["key"], (a["key"], hz)
                for k in ("up_pct", "avg_pct", "median_pct", "worst_pct", "best_pct"):
                    assert (t[k] is None) == (s[k] is None)
                    if s[k] is not None:
                        assert t[k] == pytest.approx(s[k], abs=0.011), (a["key"], hz, k)

    same(js["odds"], candles.odds(bars, filters={"trend": "down", "place": "low"}))
    same(js["plain"], candles.odds(bars))
    same(js["closed"], candles.odds(bars, closed=850))

    cone = candles.cone(bars, 10)
    assert js["cone"]["held"] == cone["held"] and js["cone"]["samples"] == cone["samples"]
    for a, b in zip(js["cone"]["steps"], cone["steps"], strict=True):
        for k in ("q10", "q25", "q50", "q75", "q90"):
            assert a[k] == pytest.approx(b[k], rel=1e-9)
