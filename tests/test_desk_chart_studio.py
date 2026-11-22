"""Charts: saved chart settings (indicators, colours) and the drawing tools on the server, and the browser's
indicators against the desk's own formulas."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from desk import indicators
from desk.chartstore import KINDS, ChartStore, clean_layout
from desk.settings import Settings

ROOT = Path(__file__).resolve().parents[1]
FAKE = {"command": sys.executable, "args": ["-m", "desk.dev.fake_kite_mcp"]}


def test_every_drawing_kind_and_its_extras_are_kept(tmp_path):
    store = ChartStore(tmp_path / "c.json")
    items = [{"kind": kind, "points": [{"time": i, "price": 100 + i} for i in range(n)]} for kind, n in KINDS.items()]
    items[0]["color"] = "#12abEF"
    long = next(i for i in items if i["kind"] == "long")
    long["qty"] = 25
    saved = store.set_drawings("india", "RELIANCE", items)
    assert [d["kind"] for d in saved] == list(KINDS)
    assert saved[0]["color"] == "#12abEF"
    assert next(d for d in saved if d["kind"] == "long")["qty"] == 25
    assert next(d for d in saved if d["kind"] == "short")["qty"] == 1          # a position always has a quantity
    assert "color" not in saved[1]
    with pytest.raises(ValueError, match="colour"):
        store.set_drawings("india", "RELIANCE", [{"kind": "hline", "points": [{"time": 1, "price": 5}], "color": "red"}])
    with pytest.raises(ValueError, match="quantity"):
        store.set_drawings("india", "RELIANCE", [{**long, "qty": -3}])
    with pytest.raises(ValueError, match="3 point"):
        store.set_drawings("india", "RELIANCE", [{"kind": "long", "points": [{"time": 1, "price": 5}]}])
    with pytest.raises(ValueError, match="unknown drawing"):
        store.set_drawings("india", "RELIANCE", [{"kind": "circle", "points": []}])


def test_layouts_are_kept_per_view_and_checked(tmp_path):
    store = ChartStore(tmp_path / "c.json")
    assert store.layout("live") is None
    layout = {"venue": "india", "symbol": "TCS", "tf": "5m", "type": "ha", "volume": False,
              "indicators": [{"kind": "ema", "params": {"period": 20, "source": "hl2"}, "colors": {"line": "#ff0000"}, "width": 3, "hidden": True},
                             {"kind": "rsi", "params": {"period": 9}}]}
    saved = store.set_layout("classic", layout)
    assert saved["indicators"][0] == {"kind": "ema", "params": {"period": 20, "source": "hl2"}, "colors": {"line": "#ff0000"}, "width": 3, "hidden": True}
    assert saved["indicators"][1]["colors"] == {} and saved["indicators"][1]["width"] is None
    assert saved["volume"] is False
    assert store.layout("classic") == saved and store.layout("live") is None
    # the watchlist and drawings are untouched by a layout save, and survive a reload
    store.set_drawings("crypto", "BTCUSDT", [{"kind": "hline", "points": [{"time": 1, "price": 5}]}])
    store.set_layout("live", {"indicators": []})
    again = ChartStore(tmp_path / "c.json")
    assert again.layout("classic") == saved and len(again.drawings("crypto", "BTCUSDT")) == 1
    bad = [{"indicators": [{"kind": "ema"}] * 31}, {"indicators": [{"kind": "Bad Kind"}]}, {"indicators": [{"kind": "ema", "colors": {"line": "blue"}}]},
           {"indicators": [{"kind": "ema", "width": 9}]}, {"indicators": [{"kind": "ema", "params": {"period": float("nan")}}]},
           {"indicators": [{"kind": "ema", "params": {"period": True}}]}, {"venue": "forex"}, {"symbol": "X" * 41}, []]
    for layout in bad:
        with pytest.raises(ValueError):
            clean_layout(layout)
    with pytest.raises(ValueError, match="view"):
        store.set_layout("other", {})


@pytest.fixture
def client(tmp_path):
    from fastapi.testclient import TestClient

    from desk.server import create_app

    settings = Settings(tmp_path / "settings.json")
    settings.update({"mcp": {"mcpServers": {"kite": {**FAKE, "env": {"FAKE_KITE_ANCHOR": "2026-11-16"}}}}})
    app = create_app(settings=settings, token="t")
    app.state.chartstore = ChartStore(tmp_path / "charts.json")
    with TestClient(app) as c:
        c.headers["X-Desk-Token"] = "t"
        yield c


def test_layout_and_position_drawings_over_the_api(client):
    assert client.get("/api/md/layout?view=live").json() == {"layout": None}
    body = {"view": "live", "layout": {"venue": "crypto", "symbol": "BTCUSDT", "tf": "1h", "indicators": [{"kind": "macd", "params": {"fast": 8, "slow": 21, "signal": 5}}]}}
    assert client.put("/api/md/layout", json=body).json()["layout"]["indicators"][0]["params"]["fast"] == 8
    assert client.get("/api/md/layout?view=live").json()["layout"]["symbol"] == "BTCUSDT"
    bad = client.put("/api/md/layout", json={"view": "live", "layout": {"indicators": [{"kind": "ema", "width": 0}]}})
    assert bad.status_code == 400 and "thickness" in bad.json()["error"]
    assert client.get("/api/md/layout?view=nope").status_code == 400
    position = {"kind": "short", "qty": 50, "color": "#000000",
                "points": [{"time": 100, "price": 2500}, {"time": 1300, "price": 2450}, {"time": 100, "price": 2525}]}
    out = client.put("/api/md/drawings", json={"venue": "india", "symbol": "RELIANCE", "items": [position]}).json()["items"]
    assert out[0]["qty"] == 50 and out[0]["kind"] == "short"


def test_the_classic_chart_uses_the_shared_chart_code():
    html = (ROOT / "desk/static/index.html").read_text()
    assert 'id="chart-classic"' in html and 'id="form-chart"' not in html
    app = (ROOT / "desk/static/app.js").read_text()
    assert 'preset: view' in app and "loadChart(" not in app


# -- the browser's indicators match the desk's ---------------------------------------------------------

@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_more_js_indicators_match_the_python_ones():
    rng = np.random.default_rng(5)
    n = 900
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))
    open_ = close * (1 + rng.normal(0, 0.002, n))
    high = np.maximum(open_, close) * (1 + rng.uniform(0, 0.006, n))
    low = np.minimum(open_, close) * (1 - rng.uniform(0, 0.006, n))
    volume = rng.integers(100, 1000, n).astype(float)
    # 5-minute candles, 75 a day from 09:15, on weekdays only
    days = pd.bdate_range("2026-09-01", periods=n // 75 + 1)
    index = pd.DatetimeIndex([d + pd.Timedelta(minutes=555 + 5 * i) for d in days for i in range(75)][:n])
    bars = pd.DataFrame({"Open": open_, "High": high, "Low": low, "Close": close, "Volume": volume}, index=index)
    rows = [{"time": int(t.value // 10**9), "open": o, "high": h, "low": lo, "close": c, "volume": v}
            for t, o, h, lo, c, v in zip(index, open_, high, low, close, volume, strict=True)]
    # day candles starting mid-week and mid-month, so the first week and month are incomplete and set no levels
    dclose = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, 300)))
    daily = pd.DataFrame({"Open": dclose, "High": dclose * 1.01, "Low": dclose * 0.99, "Close": dclose, "Volume": 1.0},
                         index=pd.bdate_range("2025-12-17", periods=300))
    drows = [{"time": int(t.value // 10**9), "open": r.Open, "high": r.High, "low": r.Low, "close": r.Close, "volume": r.Volume}
             for t, r in zip(daily.index, daily.itertuples(), strict=True)]
    module = (ROOT / "desk/static/chartdesk/indicators.js").as_uri()
    script = """
      import * as ind from "MODULE";
      import { readFileSync } from "node:fs";
      const [bars, days] = JSON.parse(readFileSync(0, "utf8"));
      const c = (kind, p, b = bars) => ind.compute(kind, b, p, { seconds: 300 });
      const st = ind.supertrendValues(bars, 10, 3), adx = ind.adxValues(bars, 14);
      const pts = (v) => v.map((x, i) => (x == null ? null : { time: bars[i].time, value: x })).filter(Boolean);
      const piv = (b, f) => { const r = ind.pivotValues(b, f, "traditional"); return Object.fromEntries(Object.entries(r).map(([k, v]) => [k, v.map((x, i) => (x == null ? null : { time: b[i].time, value: x })).filter(Boolean)])); };
      console.log(JSON.stringify({
        supertrend: pts(st.line), adx: pts(adx.adx), plus: pts(adx.plus), minus: pts(adx.minus),
        atr: c("atr", { period: 14 }).line, stoch: c("stochastic", { period: 14, smoothK: 1, smooth: 3 }),
        cci: c("cci", { period: 20 }).line, ao: c("ao", { fast: 5, slow: 34 }).line, psar: pts(ind.psarValues(bars, 0.02, 0.2)),
        donchian: c("donchian", { period: 20 }), pivD: piv(bars, "D"), pivW: piv(days, "W"), pivM: piv(days, "M"),
      }));
    """.replace("MODULE", module)
    done = subprocess.run(["node", "--input-type=module", "-e", script], input=json.dumps([rows, drows]),
                          capture_output=True, text=True, check=True)
    js = json.loads(done.stdout)

    def check(points, series):
        expect = series.dropna()
        assert len(points) == len(expect), (len(points), len(expect))
        assert [p["time"] for p in points] == [int(t.value // 10**9) for t in expect.index]
        assert np.allclose([p["value"] for p in points], expect.to_numpy(), rtol=1e-8, atol=1e-8)

    check(js["supertrend"], indicators.supertrend(bars, 10, 3.0)[0])
    a, p, m = indicators.adx(bars, 14)
    check(js["adx"], a)
    check(js["plus"], p)
    check(js["minus"], m)
    check(js["atr"], indicators.atr(bars, 14))
    k, d = indicators.stochastic(bars, 14, 3)
    check(js["stoch"]["k"], k)
    check(js["stoch"]["d"], d)
    check(js["cci"], indicators.cci(bars, 20))
    check(js["ao"], indicators.awesome_oscillator(bars))
    check(js["psar"], indicators.parabolic_sar(bars)[0])
    hi, lo = indicators.donchian(bars, 20)
    check(js["donchian"]["upper"], hi)
    check(js["donchian"]["lower"], lo)
    for key, freq, frame in (("pivD", "D", bars), ("pivW", "W", daily), ("pivM", "M", daily)):
        levels = indicators.period_pivots(frame, freq)
        assert levels["P"].notna().any(), key
        for name in indicators.PIVOT_LEVELS:
            check(js[key][name.lower()], levels[name])
