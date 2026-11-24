"""Replay charts: the candle window around a start, blind (random) starts, and practice results kept apart."""
from __future__ import annotations

import random
import sys
from datetime import datetime

import pandas as pd
import pytest

from desk import market, replay
from desk.marketdata import IST_OFFSET
from desk.marketdata.crypto import Binance, CryptoFeed
from desk.settings import Settings

FAKE = {"command": sys.executable, "args": ["-m", "desk.dev.fake_kite_mcp"]}


def rows(n, start=1_600_000_000, step=300):
    return [{"time": start + i * step, "open": 100.0 + i, "high": 101.0 + i, "low": 99.0 + i, "close": 100.5 + i,
             "volume": 10.0} for i in range(n)]


def test_window_hides_everything_from_the_cut_on():
    data = rows(100)
    cut = data[40]["time"]
    out, shown = replay.window(data, cut, 300, warm=30, ahead=20)
    assert shown == 30 and len(out) == 50
    assert all(r["time"] + 300 <= cut for r in out[:shown]) and out[shown]["time"] == cut
    out, shown = replay.window(data, cut + 60, 300, warm=500, ahead=500)    # the 40th candle is still forming
    assert shown == 40 and len(out) == 100 and out[shown]["time"] == cut


def test_start_parsing_uses_the_session_open_and_refuses_today():
    assert replay.parse_start("2021-03-12", "5m", "india") == pd.Timestamp("2021-03-12 09:15")
    assert replay.parse_start("2021-03-12", "5m", "crypto") == pd.Timestamp("2021-03-12 05:30")
    assert replay.parse_start("2021-03-12", "1d", "india") == pd.Timestamp("2021-03-12")
    assert replay.parse_start("2021-03-12T10:45", "5m", "india") == pd.Timestamp("2021-03-12 10:45")
    for bad in ("", "12-03-2021", "2021-13-40"):
        with pytest.raises(ValueError):
            replay.parse_start(bad, "5m", "india")
    today = pd.Timestamp("2026-11-24 14:00")
    with pytest.raises(ValueError, match="in the past"):
        replay.check_start(pd.Timestamp("2026-11-24 14:05"), "india", "5m", today)
    replay.check_start(pd.Timestamp("2026-11-24 10:00"), "india", "5m", today)     # earlier today is fine
    with pytest.raises(ValueError, match="starts 02 Mar 2015"):
        replay.check_start(pd.Timestamp("2014-06-02 09:15"), "india", "5m", today)
    replay.check_start(pd.Timestamp("2014-06-02"), "india", "1d", today)


def test_random_start_is_a_weekday_with_room_after_it():
    today = pd.Timestamp("2026-11-24")
    rng = random.Random(7)
    for _ in range(50):
        s = replay.random_start("india", "5m", today, rng)
        assert s.weekday() < 5 and s >= pd.Timestamp("2015-03-02") and s < today - pd.Timedelta(days=9)
        assert (s.hour, s.minute) == (9, 15)


def test_summary_counts_net_of_charges_and_the_worst_drawdown():
    trades = [{"gross": 100, "charges": 10, "net": 90}, {"gross": -50, "charges": 10, "net": -60},
              {"gross": -50, "charges": 10, "net": -60}, {"gross": 200, "charges": 10, "net": 190}]
    s = replay.summary(trades)
    assert s["trades"] == 4 and s["wins"] == 2 and s["win_rate"] == 50.0
    assert s["gross"] == 200 and s["charges"] == 40 and s["net"] == 160 and s["max_drawdown"] == -120
    assert replay.summary([])["win_rate"] is None


def test_store_works_out_pnl_and_hides_a_blind_stock_until_ended(tmp_path):
    store = replay.ReplayStore(tmp_path / "r.json", lambda session, trade: 5.0)
    trade = {"side": "short", "qty": 10, "entry": 100, "exit": 90, "entry_time": 1, "exit_time": 2,
             "reason": "Target", "product": "intraday", "net": 99999}
    s = store.save({"venue": "india", "symbol": "reliance", "tf": "5m", "start": 1000, "blind": True, "trades": [trade]})
    t = s["trades"][0]
    assert t["gross"] == 100 and t["charges"] == 5 and t["net"] == 95          # the client's figure is not trusted
    listed = store.list()
    assert listed["sessions"][0]["symbol"] == "Hidden" and listed["score"]["net"] == 95
    store.save({**s, "ended": True})
    assert store.list()["sessions"][0]["symbol"] == "RELIANCE" and len(store.list()["sessions"]) == 1
    for bad in ({**s, "venue": "nse"}, {**s, "trades": [{**trade, "side": "up"}]},
                {**s, "trades": [{**trade, "qty": 0}]}, {**s, "trades": [{**trade, "reason": "Luck"}]}):
        with pytest.raises(ValueError):
            store.save(bad)
    assert store.delete(s["id"]) and not store.delete(s["id"])


def test_binance_history_pages_from_the_start():
    asked = []

    def http(url, params):
        asked.append(dict(params))
        start = params["startTime"] // 1000
        n = 1000 if len(asked) == 1 else 200
        return 200, [[(start + i * 60) * 1000, "1", "2", "0.5", "1.5", "3"] for i in range(n)]

    out = Binance(http).history("BTCUSDT", "1m", 1_600_000_000, 1100)
    assert len(out) == 1100 and out[0]["time"] == 1_600_000_000 + IST_OFFSET
    assert asked[1]["startTime"] == (1_600_000_000 + 999 * 60) * 1000 + 1
    assert [r["time"] for r in out] == sorted(r["time"] for r in out)


# -- through the API ----------------------------------------------------------------------------------

@pytest.fixture
def client(tmp_path):
    from fastapi.testclient import TestClient

    from desk.server import create_app

    settings = Settings(tmp_path / "settings.json")
    settings.update({"mcp": {"mcpServers": {"kite": {**FAKE, "env": {"FAKE_KITE_ANCHOR": "2026-11-16"}}}},
                     "enforce_trade_window": False})
    app = create_app(settings=settings, token="t")
    app.state.derivatives._now = lambda: datetime(2026, 11, 16, 10, 0, tzinfo=market.IST)
    with TestClient(app, headers={"X-Desk-Token": "t"}) as test_client:
        yield test_client
    app.state.kite.disconnect()


def test_api_india_replay_needs_the_broker_then_cuts_at_the_start(client):
    r = client.get("/api/md/replay?venue=india&symbol=RELIANCE&tf=1d&start=2025-06-02")
    assert r.status_code == 502 and r.json()["needs_login"]
    client.post("/api/kite/connect")
    client.post("/api/kite/login")
    for tf, start in (("1d", "2025-06-02"), ("15m", "2026-10-05"), ("1h", "2026-09-01")):
        out = client.get(f"/api/md/replay?venue=india&symbol=RELIANCE&tf={tf}&start={start}").json()
        assert out.get("source") == "Kite", out
        cut, shown = out["start"], out["start_index"]
        times = [c["time"] for c in out["candles"]]
        sec = out["seconds"]
        assert times == sorted(times) and shown >= 20 and times[shown - 1] + sec <= cut < times[shown] + sec
        assert out["live"] == {"kind": "none"}
        more = client.get(f"/api/md/replay?venue=india&symbol=RELIANCE&tf={tf}&after={times[-1]}").json()
        assert all(c["time"] > times[-1] for c in more["candles"])
    assert client.get("/api/md/replay?venue=india&symbol=RELIANCE&tf=5m&start=2099-01-01").status_code == 400
    blind = client.get("/api/md/replay?venue=india&symbol=RELIANCE&tf=1d&start=random")
    assert blind.status_code == 200, blind.json()


def test_api_crypto_replay_and_practice_sessions(client):
    def http(url, params):
        start = params["startTime"] // 1000
        return 200, [[(start + i * 3600) * 1000, "10", "12", "9", "11", "3"] for i in range(1000)]

    client.app.state.crypto = CryptoFeed(http)
    out = client.get("/api/md/replay?venue=crypto&symbol=BTCUSDT&tf=1h&start=2024-03-01").json()
    assert out["source"] == "Binance" and out["start_index"] == replay.WARM_BARS
    assert out["candles"][out["start_index"]]["time"] == out["start"]
    trade = {"side": "long", "qty": 0.5, "entry": 100, "exit": 110, "entry_time": 1, "exit_time": 2,
             "reason": "Target", "product": "delivery"}
    saved = client.post("/api/replay/sessions", json={"venue": "crypto", "symbol": "BTCUSDT", "tf": "1h",
                                                      "start": out["start"], "trades": [trade]}).json()
    t = saved["session"]["trades"][0]
    assert t["gross"] == 5 and t["charges"] == pytest.approx(0.1, abs=0.006)     # 0.1% a side, as crypto paper
    sid = saved["session"]["id"]
    assert client.get(f"/api/replay/sessions/{sid}").json()["session"]["id"] == sid
    india = client.post("/api/replay/sessions", json={"venue": "india", "symbol": "RELIANCE", "tf": "5m", "start": 1,
                                                      "trades": [{**trade, "qty": 10, "product": "intraday"}]}).json()
    assert india["session"]["trades"][0]["charges"] > 0          # brokerage, STT and the rest, as the paper desk books
    assert client.delete(f"/api/replay/sessions/{sid}").status_code == 200
    assert client.get(f"/api/replay/sessions/{sid}").status_code == 404
    # practice never touches the paper book
    assert client.get("/api/cpaper").json()["orders"] == []
