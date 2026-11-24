"""The index strip along the top of every page: one cached Kite quote call, plain states, chosen indices."""

import sys
from datetime import datetime

import pandas as pd
import pytest

pytest.importorskip("fastapi")

from desk import market, ticker as tk
from desk.settings import Settings

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("BHARAT_DESK_HOME", str(tmp_path / "desk"))
    holidays = set(market.HOLIDAYS)
    market.set_holidays([])
    yield
    market.set_holidays(holidays)


class FakeKite:
    def __init__(self, prices):
        self.prices, self.calls, self.connected, self.tools = prices, [], True, {"get_quotes", "get_ltp"}

    def quotes(self, names):
        self.calls.append(list(names))
        return {n: {"last_price": p, "ohlc": {"close": p / 1.01}} for n, p in self.prices.items() if n in names}


class FakeBoard:
    """The parts of desk.indices.Indices the strip reads."""

    def __init__(self, kite):
        self.kite = kite
        self.entries_ = [("nifty50", "Nifty 50", "Broad market", "f", ["NIFTY 50"], "^NSEI"),
                         ("bsesensex", "BSE Sensex", "BSE", "bse", ["BSE:SENSEX"], "^BSESN"),
                         ("niftyit", "Nifty IT", "Sectoral", "f", ["NIFTY IT"], None)]
        frame = pd.DataFrame({"Close": [100.0, 102.0]}, index=pd.to_datetime(["2026-11-20", "2026-11-23"]))
        self.frames = {"nifty50": frame}

    def entries(self):
        return self.entries_

    def entry(self, index_id):
        return next((e for e in self.entries_ if e[0] == index_id), None)

    def kite_name(self, index_id):
        return {"bsesensex": "BSE:SENSEX"}.get(index_id)          # nifty50 not resolved yet: first spelling

    def level(self, index_id):
        return self.frames.get(index_id), "Yahoo"


def at(text):
    moment = datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=market.IST)
    return lambda: moment


def test_one_kite_call_serves_the_strip_and_states_are_plain(monkeypatch):
    kite = FakeKite({"NSE:NIFTY 50": 24318.45, "BSE:SENSEX": 80104.2, "NSE:INDIA VIX": 14.62})
    board = FakeBoard(kite)
    chosen = ["nifty50", "bsesensex", "indiavix", "gone_index"]
    strip = tk.Ticker(board, lambda: chosen, now=at("2026-11-24 11:00"))     # a Tuesday, market open
    snap = strip.snapshot()
    assert snap["state"] == "live" and snap["every"] == tk.LIVE_SECONDS and snap["as_of"]
    assert [i["label"] for i in snap["items"]] == ["NIFTY 50", "SENSEX", "INDIA VIX"]       # unknown id skipped
    nifty = snap["items"][0]
    assert nifty["value"] == 24318.45 and nifty["change_pct"] == pytest.approx(1.0, abs=0.01) and nifty["kite"]
    assert nifty["chart"] == "NSE:NIFTY 50" and snap["items"][1]["chart"] == "IDX:bsesensex"     # unnamed: its Kite spelling
    assert snap["items"][2]["chart"] == "NSE:INDIA VIX"
    assert kite.calls == [["NSE:NIFTY 50", "BSE:SENSEX", "NSE:INDIA VIX"]]
    strip.snapshot(), strip.snapshot()
    assert len(kite.calls) == 1                                             # cached: no new call inside 15 seconds
    strip._at -= tk.LIVE_SECONDS + 1
    strip._tried -= tk.LIVE_SECONDS + 1
    strip.snapshot()
    assert len(kite.calls) == 2
    # Kite stops answering: the values age into "stale", with the time they are from.
    kite.quotes = lambda names: (_ for _ in ()).throw(OSError("down"))
    strip._at -= tk.FRESH_SECONDS + 1
    strip._tried -= tk.LIVE_SECONDS + 1
    assert strip.snapshot()["state"] == "stale"
    # After the close: the last values, asked for rarely.
    closed = tk.Ticker(board, lambda: chosen, now=at("2026-11-24 18:00"))
    kite.quotes = FakeKite.quotes.__get__(kite)
    snap = closed.snapshot()
    assert snap["state"] == "closed" and snap["every"] == tk.CLOSED_SECONDS and snap["items"][0]["value"] == 24318.45


def test_without_kite_the_strip_shows_last_closes_and_says_offline():
    kite = FakeKite({})
    kite.connected = False
    strip = tk.Ticker(FakeBoard(kite), lambda: ["nifty50", "indiavix", "niftyit"], now=at("2026-11-24 11:00"))
    snap = strip.snapshot()
    assert snap["state"] == "offline" and kite.calls == []
    nifty, vix, it = snap["items"]
    assert nifty["value"] == 102.0 and nifty["change"] == 2.0 and nifty["change_pct"] == 2.0 and not nifty["kite"]
    assert vix["value"] is None and it["value"] is None                    # nothing to show yet, still listed


def test_choices_and_the_setting():
    strip = tk.Ticker(FakeBoard(FakeKite({})), lambda: [])
    choices = {c["id"]: c for c in strip.choices()}
    assert choices["indiavix"]["group"] == "Main" and choices["bsesensex"]["label"] == "SENSEX"
    assert choices["niftyit"]["label"] == "NIFTY IT"
    assert tk.validate("nifty50, bsesensex nifty50") == ["nifty50", "bsesensex"]
    with pytest.raises(ValueError):
        tk.validate(["NIFTY;50"])
    with pytest.raises(ValueError):
        tk.validate(["x"] * (tk.MAX_INDICES + 1))
    from desk.settings import DEFAULTS

    assert DEFAULTS["ticker_indices"] == tk.DEFAULT and DEFAULTS["ticker_show"] is True


def test_api_ticker_follows_the_settings(tmp_path):
    from fastapi.testclient import TestClient

    from desk.server import create_app

    settings = Settings(tmp_path / "settings.json")
    fake = {"command": sys.executable, "args": ["-m", "desk.dev.fake_kite_mcp"]}
    settings.update({"mcp": {"mcpServers": {"kite": fake}}})
    app = create_app(settings=settings, token="t", fetch=lambda symbol, interval="1d": None)
    with TestClient(app, headers={"X-Desk-Token": "t"}) as client:
        snap = client.get("/api/ticker").json()
        assert snap["state"] in ("live", "closed", "stale", "offline")
        assert [i["id"] for i in snap["items"]] == tk.DEFAULT
        choices = client.get("/api/ticker/choices").json()
        assert choices["chosen"] == tk.DEFAULT and any(c["id"] == "niftyit" for c in choices["choices"])
        assert client.put("/api/settings", json={"ticker_indices": ["niftyit", "nifty50"]}).status_code == 200
        assert [i["label"] for i in client.get("/api/ticker").json()["items"]] == ["NIFTY IT", "NIFTY 50"]
        assert client.put("/api/settings", json={"ticker_indices": ["bad id"]}).status_code >= 400
        client.put("/api/settings", json={"ticker_show": False})
        assert client.get("/api/ticker").json() == {"state": "hidden", "items": [], "every": 300}
    app.state.kite.disconnect()
