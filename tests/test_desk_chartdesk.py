"""Charts tab: crypto providers with fallback, broker candles (no Yahoo), the API, and JS indicator parity."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from desk import indicators, market
from desk.marketdata import IST_OFFSET, MarketDataError, merge_rows, weekly
from desk.marketdata.crypto import Binance, Coinbase, CryptoFeed
from desk.settings import Settings

FAKE = {"command": sys.executable, "args": ["-m", "desk.dev.fake_kite_mcp"]}
ROOT = Path(__file__).resolve().parents[1]
NOW = 1_763_800_000          # a fixed "now" (UTC seconds)


def klines(n=50, start=NOW - 50 * 3600, step=3600, base=100.0):
    return [[(start + i * step) * 1000, str(base + i), str(base + i + 2), str(base + i - 1), str(base + i + 1), "10",
             0, "0", 0, "0", "0", "0"] for i in range(n)]


def cb_rows(n, end, step, base=50.0):
    # Coinbase: [time, low, high, open, close, volume], newest first
    return [[end - i * step, base - 1, base + 2, base, base + 1, 3.0] for i in range(1, n + 1)]


class Http:
    """Answers by host; records what was asked."""

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def __call__(self, url, params):
        self.calls.append((url, dict(params)))
        for prefix, answer in self.routes.items():
            if url.startswith(prefix):
                return answer(url, params) if callable(answer) else answer
        raise OSError("unreachable")


def test_binance_rows_are_parsed_onto_the_india_clock():
    http = Http({"https://data-api.binance.vision": (200, klines(3))})
    rows = Binance(http).candles("BTCUSDT", "1h")
    assert len(rows) == 3 and rows[0]["time"] == NOW - 50 * 3600 + IST_OFFSET
    assert rows[0] == {"time": rows[0]["time"], "open": 100.0, "high": 102.0, "low": 99.0, "close": 101.0, "volume": 10.0}
    assert http.calls[0][1] == {"symbol": "BTCUSDT", "interval": "1h", "limit": 1000}


def test_binance_tries_its_second_host_then_reports_both():
    http = Http({"https://data-api.binance.vision": (451, {"msg": "restricted"}), "https://api.binance.com": (200, klines(2))})
    assert len(Binance(http).candles("ETHUSDT", "5m")) == 2
    http = Http({"https://data-api.binance.vision": (451, {}), "https://api.binance.com": (429, {})})
    with pytest.raises(MarketDataError) as err:
        Binance(http).candles("ETHUSDT", "5m")
    assert "451" in str(err.value) and "429" in str(err.value) and err.value.provider == "Binance"


def test_feed_falls_back_to_coinbase_and_says_so():
    http = Http({"https://data-api.binance.vision": (451, {}), "https://api.binance.com": (451, {}),
                 "https://api.exchange.coinbase.com": lambda url, p: (200, cb_rows(5, NOW, 3600))})
    feed = CryptoFeed(http, now=lambda: NOW)
    out = feed.candles("BTCUSDT", "1h")
    assert out["source"] == "Coinbase" and out["live"]["kind"] == "coinbase" and out["live"]["product"] == "BTC-USD"
    assert "Coinbase BTC-USD" in out["notes"][0] and "451" in out["notes"][0]
    times = [r["time"] for r in out["candles"]]
    assert times == sorted(times) and len(times) == 5
    assert http.calls[-1][0].endswith("/products/BTC-USD/candles")


def test_feed_skips_a_failed_binance_for_a_minute_and_caches():
    clock = [NOW]
    http = Http({"https://data-api.binance.vision": (451, {}), "https://api.binance.com": (451, {}),
                 "https://api.exchange.coinbase.com": (200, cb_rows(5, NOW, 3600))})
    feed = CryptoFeed(http, now=lambda: clock[0])
    feed.candles("BTCUSDT", "1h")
    asked = len(http.calls)
    feed.candles("BTCUSDT", "1h")
    assert len(http.calls) == asked, "a second request within 3 s is served from the cache"
    clock[0] += 10
    feed.candles("BTCUSDT", "1h")
    assert not any("binance" in u for u, _ in http.calls[asked:]), "Binance is not retried within a minute"
    clock[0] += 61
    feed.candles("BTCUSDT", "1h")
    assert any("binance" in u for u, _ in http.calls[asked:])


def test_no_fallback_and_both_failing_are_errors_never_empty():
    down = Http({})
    with pytest.raises(MarketDataError, match="Coinbase does not list BNB"):
        CryptoFeed(down, now=lambda: NOW).candles("BNBUSDT", "1h")
    with pytest.raises(MarketDataError, match="Fallback failed too"):
        CryptoFeed(down, now=lambda: NOW).candles("BTCUSDT", "1h")
    empty = Http({"https://data-api.binance.vision": (200, []), "https://api.binance.com": (200, []),
                  "https://api.exchange.coinbase.com": (200, [])})
    with pytest.raises(MarketDataError, match="no candles"):
        CryptoFeed(empty, now=lambda: NOW).candles("BTCUSDT", "1h")
    with pytest.raises(ValueError, match="not in the crypto list"):
        CryptoFeed(down).candles("FAKECOIN", "1h")
    with pytest.raises(ValueError, match="timeframe"):
        CryptoFeed(down).candles("BTCUSDT", "3m")


def test_coinbase_builds_4_hour_and_weekly_candles():
    hourly = Http({"https://api.exchange.coinbase.com": lambda url, p: (200, cb_rows(8, 86400 * 20 + 8 * 3600, 3600))})
    rows = Coinbase(hourly, now=lambda: NOW).candles("BTC-USD", "4h")
    assert [(r["time"] - IST_OFFSET) % 14400 for r in rows] == [0, 0]     # 00:00 and 04:00 UTC
    assert rows[0]["volume"] == 12.0
    week = merge_rows([{"time": IST_OFFSET + 86400 * d, "open": d, "high": d, "low": d, "close": d, "volume": 1.0}
                       for d in range(4, 18)], 604800, 4 * 86400 + IST_OFFSET)
    assert [w["open"] for w in week] == [4, 11] and [w["close"] for w in week] == [10, 17]


def test_weekly_merges_days_from_monday():
    days = pd.date_range("2026-11-02", periods=10, freq="D")      # Monday
    frame = pd.DataFrame({"Open": range(10), "High": range(10), "Low": range(10), "Close": range(10), "Volume": 1.0}, index=days)
    out = weekly(frame)
    assert list(out.index.weekday) == [0, 0] and out["Open"].tolist() == [0, 7] and out["Volume"].tolist() == [7, 3]


# -- through the API ----------------------------------------------------------------------------------

@pytest.fixture
def client(tmp_path):
    from fastapi.testclient import TestClient

    from desk.server import create_app

    settings = Settings(tmp_path / "settings.json")
    settings.update({"mcp": {"mcpServers": {"kite": {**FAKE, "env": {"FAKE_KITE_ANCHOR": "2026-11-16"}}}},
                     "enforce_trade_window": False})

    def no_yahoo(*args, **kwargs):
        raise AssertionError("the live chart must never ask Yahoo")

    app = create_app(settings=settings, token="t", fetch=no_yahoo)
    app.state.derivatives._now = lambda: datetime(2026, 11, 16, 10, 0, tzinfo=market.IST)
    with TestClient(app, headers={"X-Desk-Token": "t"}) as test_client:
        yield test_client
    app.state.kite.disconnect()


def test_api_crypto_candles_and_errors(client):
    client.app.state.crypto = CryptoFeed(Http({"https://data-api.binance.vision": (200, klines(40))}), now=lambda: NOW)
    catalog = client.get("/api/md/catalog").json()
    assert list(catalog["timeframes"]) == ["1m", "5m", "15m", "1h", "4h", "1d", "1w"]
    assert any(c["symbol"] == "BTCUSDT" for c in catalog["crypto"])
    out = client.get("/api/md/candles?venue=crypto&symbol=BTCUSDT&tf=1h").json()
    assert out["source"] == "Binance" and len(out["candles"]) == 40 and out["live"]["url"].endswith("btcusdt@kline_1h")
    assert out["session_offset"] == IST_OFFSET
    client.app.state.crypto = CryptoFeed(Http({}), now=lambda: NOW)
    failed = client.get("/api/md/candles?venue=crypto&symbol=BTCUSDT&tf=1h")
    assert failed.status_code == 502 and "Binance did not answer" in failed.json()["error"]
    assert client.get("/api/md/candles?venue=crypto&symbol=BTCUSDT&tf=2h").status_code == 400


def test_api_india_needs_the_broker_and_never_yahoo(client):
    r = client.get("/api/md/candles?venue=india&symbol=RELIANCE&tf=1d")
    assert r.status_code == 502 and r.json()["needs_login"] and "log in to Kite" in r.json()["error"]
    assert client.get("/api/md/quote?symbol=RELIANCE").status_code == 502
    client.post("/api/kite/connect")
    client.post("/api/kite/login")
    for tf in ("1d", "1w", "1h", "4h", "15m"):
        out = client.get(f"/api/md/candles?venue=india&symbol=RELIANCE&tf={tf}").json()
        assert out.get("source") == "Kite", out
        times = [c["time"] for c in out["candles"]]
        assert times == sorted(times) and all(c["close"] > 0 for c in out["candles"])
        if tf == "4h":
            clock = {(t % 86400) for t in times}
            assert clock <= {9 * 3600 + 15 * 60, 13 * 3600 + 15 * 60}, clock
        if tf == "1w":
            assert {pd.Timestamp(t, unit="s").weekday() for t in times} == {0}
    assert client.get("/api/md/quote?symbol=RELIANCE").json()["price"] > 0


def test_csp_allows_only_the_two_crypto_streams(client):
    policy = client.get("/api/state").headers["content-security-policy"]
    connect = next(p for p in policy.split(";") if "connect-src" in p).split()[1:]
    assert connect == ["'self'", "wss://data-stream.binance.vision", "wss://ws-feed.exchange.coinbase.com"]


# -- the browser's indicators match the desk's ---------------------------------------------------------

@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_js_indicators_match_the_python_ones():
    rng = np.random.default_rng(3)
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, 300)))
    high, low = close * 1.01, close * 0.99
    volume = rng.integers(100, 1000, 300).astype(float)
    index = pd.date_range("2026-11-02 09:15", periods=300, freq="5min")
    bars = pd.DataFrame({"Open": close, "High": high, "Low": low, "Close": close, "Volume": volume}, index=index)
    rows = [{"time": int(t.value // 10**9), "open": o, "high": h, "low": lo, "close": c, "volume": v}
            for t, o, h, lo, c, v in zip(index, close, high, low, close, volume, strict=True)]
    module = (ROOT / "desk/static/chartdesk/indicators.js").as_uri()
    script = """
      import * as ind from "MODULE";
      const bars = JSON.parse(process.argv[1]);
      const out = { sma: ind.sma(bars, {period: 20}).line, ema: ind.ema(bars, {period: 20}).line,
        rsi: ind.rsi(bars, {period: 14}).line, bb: ind.bollinger(bars, {period: 20, width: 2}),
        macd: ind.macd(bars, {fast: 12, slow: 26, signal: 9}), vwap: ind.vwap(bars, {sessionOffset: 0}).line };
      console.log(JSON.stringify(out));
    """.replace("MODULE", module)
    done = subprocess.run(["node", "--input-type=module", "-e", script, json.dumps(rows)], capture_output=True, text=True, check=True)
    js = json.loads(done.stdout)

    def check(points, series):
        expect = series.dropna()
        assert len(points) == len(expect)
        assert np.allclose([p["value"] for p in points], expect.to_numpy(), rtol=1e-9, atol=1e-9)

    check(js["sma"], indicators.sma(bars["Close"], 20))
    check(js["ema"], indicators.ema(bars["Close"], 20))
    check(js["rsi"], indicators.rsi(bars["Close"], 14))
    upper, mid, lower = indicators.bollinger(bars["Close"], 20, 2.0)
    for key, series in (("upper", upper), ("middle", mid), ("lower", lower)):
        check(js["bb"][key], series)
    line, sig, hist = indicators.macd(bars["Close"])
    for key, series in (("macd", line), ("signal", sig), ("histogram", hist)):
        check(js["macd"][key], series)
    check(js["vwap"], indicators.vwap(bars))
