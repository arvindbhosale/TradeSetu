"""Direct Kite Connect: one login a day, saved across restarts, in kite-mcp-server's shapes.

Nothing here reaches Zerodha. The HTTP answers follow Kite Connect's documented shapes.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import pytest

from desk import kiteconnect
from desk.kite import KiteError
from desk.kiteconnect import KiteDirect, KiteLink, request_token_from, token_expiry

IST = timezone(timedelta(hours=5, minutes=30))
CSV = ("instrument_token,exchange_token,tradingsymbol,name,last_price,expiry,strike,tick_size,lot_size,"
       "instrument_type,segment,exchange\n"
       "738561,2885,RELIANCE,RELIANCE INDUSTRIES,0,,0,0.05,1,EQ,NSE,NSE\n"
       "256265,1001,NIFTY 50,NIFTY 50,0,,0,0,0,EQ,INDICES,NSE\n"
       "12345678,48225,NIFTY25NOVFUT,NIFTY,0,2025-11-25,0,0.1,75,FUT,NFO-FUT,NFO\n"
       "12345679,48226,NIFTY25NOV24000CE,NIFTY,0,2025-11-25,24000,0.05,75,CE,NFO-OPT,NFO\n")


class FakeKite:
    """Kite Connect's REST answers, recording every call."""

    def __init__(self, key="kitekey1", secret="kitesecret1"):
        self.key, self.secret, self.calls, self.token = key, secret, [], "accesstoken123"
        self.refuse = False

    def __call__(self, method, url, params=None, data=None, json=None, headers=None):
        path = urlparse(url).path
        self.calls.append((method, path, params, data, json, headers))
        if path == "/session/token" and method == "POST":
            want = hashlib.sha256((self.key + data["request_token"] + self.secret).encode()).hexdigest()
            if data["checksum"] != want:
                return 403, {"status": "error", "message": "Invalid checksum", "error_type": "TokenException"}, {}
            return 200, {"status": "success", "data": {"access_token": self.token, "user_id": "AB1234",
                                                       "user_name": "Test User"}}, {}
        if headers.get("Authorization") != f"token {self.key}:{self.token}" or self.refuse:
            return 403, {"status": "error", "message": "Incorrect api_key or access_token.",
                         "error_type": "TokenException"}, {}
        if path == "/user/profile":
            return 200, {"status": "success", "data": {"user_id": "AB1234"}}, {}
        if path == "/portfolio/positions":
            return 200, {"status": "success", "data": {"net": [{"tradingsymbol": "INFY"}], "day": []}}, {}
        if path == "/instruments":
            return 200, CSV.encode(), {}
        if path.startswith("/instruments/historical/"):
            return 200, {"status": "success", "data": {"candles": [
                ["2025-11-14T09:15:00+0530", 100, 101, 99, 100.5, 1000, 50],
                ["2025-11-14T09:16:00+0530", 100.5, 102, 100, 101.5, 900, 55]]}}, {}
        if path == "/gtt/triggers" and method == "POST":
            return 200, {"status": "success", "data": {"trigger_id": 42}}, {}
        if path == "/orders/regular" and method == "POST":
            return 200, {"status": "success", "data": {"order_id": "2511"}}, {}
        if path == "/margins/basket":
            return 200, {"status": "success", "data": {"initial": {"total": 200}, "final": {"total": 150},
                                                       "orders": [{"total": 200}]}}, {}
        return 404, {"status": "error", "message": "not here"}, {}


@pytest.fixture
def clock(monkeypatch):
    now = {"t": datetime(2025, 11, 14, 9, 0, tzinfo=IST)}
    monkeypatch.setattr(kiteconnect, "_ist_now", lambda: now["t"])
    return now


def signed_in(tmp_path, fake, clock):
    kite = KiteDirect(session_path=tmp_path / "kite-session.json", http=fake, instruments_dir=tmp_path)
    kite.connect({"api_key": fake.key, "api_secret": fake.secret, "redirect_uri": "http://127.0.0.1:47800/broker/callback"})
    login = kite.login()
    state = parse_qs(parse_qs(urlparse(login["url"]).query)["redirect_params"][0])["state"][0]
    kite.complete_login({"request_token": "reqtoken99", "status": "success", "state": state})
    return kite


def test_token_lasts_until_six_the_next_morning():
    assert token_expiry(datetime(2025, 11, 14, 9, 30, tzinfo=IST)) == datetime(2025, 11, 15, 6, 0, tzinfo=IST)
    assert token_expiry(datetime(2025, 11, 14, 5, 30, tzinfo=IST)) == datetime(2025, 11, 14, 6, 0, tzinfo=IST)


def test_request_token_from_a_pasted_address():
    assert request_token_from("http://127.0.0.1:47800/broker/callback?action=login&request_token=abc123XYZ&status=success") == "abc123XYZ"
    assert request_token_from(" abc123XYZ ") == "abc123XYZ"
    with pytest.raises(KiteError):
        request_token_from("http://127.0.0.1:47800/broker/callback?status=cancelled")


def test_one_login_is_saved_and_survives_a_restart(tmp_path, clock):
    fake = FakeKite()
    kite = signed_in(tmp_path, fake, clock)
    status = kite.status()
    assert status["signed_in"] and status["direct"] and status["user_id"] == "AB1234"
    assert status["session_until"] == "2025-11-15 06:00 IST"
    saved = json.loads((tmp_path / "kite-session.json").read_text())
    assert saved["access_token"] == "accesstoken123" and saved["api_key"] == fake.key
    assert kite.login()["url"] is None                      # already logged in: no second login today

    clock["t"] += timedelta(hours=8)                        # the app restarts in the afternoon
    again = KiteDirect(session_path=tmp_path / "kite-session.json", http=fake, instruments_dir=tmp_path)
    assert again.connect({"api_key": fake.key, "api_secret": fake.secret})["signed_in"]
    assert again.call("get_profile") == {"user_id": "AB1234"}
    assert again.call("get_positions")["net"][0]["tradingsymbol"] == "INFY"
    assert not [c for c in fake.calls[1:] if c[1] == "/session/token"]      # no new token exchange

    clock["t"] = datetime(2025, 11, 15, 6, 0, tzinfo=IST)   # 6 AM: Kite's token is over
    assert not again.signed_in
    with pytest.raises(KiteError) as info:
        again.profile()
    assert info.value.needs_login
    assert not (tmp_path / "kite-session.json").exists()


def test_a_saved_login_for_another_api_key_is_not_used(tmp_path, clock):
    signed_in(tmp_path, FakeKite(), clock)
    other = KiteDirect(session_path=tmp_path / "kite-session.json", http=FakeKite("otherkey1"))
    assert not other.connect({"api_key": "otherkey1", "api_secret": "x"})["signed_in"]


def test_a_refused_token_asks_for_a_new_login(tmp_path, clock):
    fake = FakeKite()
    kite = signed_in(tmp_path, fake, clock)
    fake.refuse = True
    with pytest.raises(KiteError) as info:
        kite.holdings()
    assert info.value.needs_login and not kite.signed_in and not (tmp_path / "kite-session.json").exists()


def test_forged_or_stale_redirects_are_refused(tmp_path, clock):
    kite = KiteDirect(session_path=tmp_path / "s.json", http=FakeKite())
    kite.connect({"api_key": "kitekey1", "api_secret": "kitesecret1"})
    kite.login()
    with pytest.raises(KiteError, match="stale"):
        kite.complete_login({"request_token": "reqtoken99", "state": "forged"})
    assert not kite.signed_in


def test_orders_gtt_margin_candles_and_instruments(tmp_path, clock):
    fake = FakeKite()
    kite = signed_in(tmp_path, fake, clock)
    assert kite.call("place_order", {"variety": "regular", "exchange": "NSE", "tradingsymbol": "INFY",
                                     "transaction_type": "BUY", "quantity": 1, "product": "CNC",
                                     "order_type": "LIMIT", "price": 1500.5, "trigger_price": 0}) == {"order_id": "2511"}
    sent = fake.calls[-1][3]
    assert sent["price"] == "1500.5" and "trigger_price" not in sent and "variety" not in sent

    gtt = kite.place_gtt({"exchange": "NSE", "tradingsymbol": "INFY", "last_price": 1500, "transaction_type": "SELL",
                          "product": "CNC", "trigger_type": "two-leg", "lower_trigger_value": 1400,
                          "lower_limit_price": 1399, "lower_quantity": 5, "upper_trigger_value": 1700,
                          "upper_limit_price": 1701, "upper_quantity": 5})
    assert gtt == {"trigger_id": 42}
    form = fake.calls[-1][3]
    assert form["type"] == "two-leg" and json.loads(form["condition"])["trigger_values"] == [1400.0, 1700.0]
    assert [o["price"] for o in json.loads(form["orders"])] == [1399.0, 1701.0]

    margin = kite.call("get_basket_margins", {"orders": [{"exchange": "NFO"}], "consider_positions": False})
    assert margin["final"]["total"] == 150 and fake.calls[-1][2] == {"consider_positions": "false"}

    rows = kite.search("FUT", "tradingsymbol")
    assert [r["id"] for r in rows] == ["NFO:NIFTY25NOVFUT"] and rows[0]["expiry_date"] == "2025-11-25"
    assert {r["id"] for r in kite.call("search_instruments", {"query": "NFO:NIFTY", "filter_on": "underlying"})} == {
        "NFO:NIFTY25NOVFUT", "NFO:NIFTY25NOV24000CE"}
    assert kite.search("NSE:NIFTY 50")[0]["segment"] == "INDICES"

    candles = kite.candles("NSE:RELIANCE", "1m", datetime(2025, 11, 14, 9, 15), datetime(2025, 11, 14, 15, 30), oi=True)
    assert candles[0] == {"date": "2025-11-14T09:15:00+0530", "open": 100, "high": 101, "low": 99, "close": 100.5,
                          "volume": 1000, "oi": 50}
    assert "/instruments/historical/738561/minute" in fake.calls[-1][1]


def test_link_picks_direct_with_keys_and_mcp_without(tmp_path):
    class Mcp:
        connected = False

        def connect(self, entry, timeout=0):
            self.connected, self.entry = True, entry
            return {"connected": True}

        def disconnect(self):
            self.connected = False

        def status(self):
            return {"connected": self.connected}

    link = KiteLink(mcp=Mcp(), direct=KiteDirect(session_path=tmp_path / "s.json", http=FakeKite()))
    link.connect({"url": "http://kite-mcp:8080/mcp"})
    assert not link.is_direct and link.status()["direct"] is False
    link.connect({"api_key": "kitekey1", "api_secret": "kitesecret1"})
    assert link.is_direct and not link.mcp.connected and link.status()["direct"]


def test_app_settings_connect_callback_and_restart(tmp_path, monkeypatch, clock):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from desk.server import create_app
    from desk.settings import Settings

    monkeypatch.setenv("TRADESETU_HOME", str(tmp_path))
    fake = FakeKite()
    settings = Settings(tmp_path / "settings.json")
    public = settings.update({"kite_connect": {"api_key": fake.key, "api_secret": fake.secret}})
    assert public["kite_connect"]["api_secret"].startswith("•") and fake.secret not in json.dumps(public)
    settings.update({"kite_connect": {"api_key": fake.key, "api_secret": public["kite_connect"]["api_secret"]}})
    assert settings.get("kite_connect")["api_secret"] == fake.secret       # masked value sent back: kept
    with pytest.raises(ValueError):
        settings.update({"kite_connect": {"api_key": "has spaces in it"}})

    def build():
        app = create_app(settings=settings, fetch=lambda *a, **k: None, token="t")
        link = app.state.kite.kite if hasattr(app.state.kite, "kite") else app.state.kite
        link.direct._http = fake
        return app, TestClient(app)

    app, client = build()
    headers = {"x-desk-token": "t"}
    status = client.post("/api/kite/connect", headers=headers).json()
    assert status["direct"] and not status["signed_in"]
    assert status["redirect_uri"] == "http://testserver/broker/callback"
    login = client.post("/api/kite/login", headers=headers).json()
    assert "api_key=kitekey1" in login["url"]
    assert client.get("/broker/callback?request_token=x&status=success&state=forged").status_code == 400
    client.post("/api/kite/login", headers=headers)                          # a fresh link replaces the old one
    state = app.state.kite.direct._state
    page = client.get(f"/broker/callback?request_token=reqtoken99&action=login&status=success&state={state}")
    assert page.status_code == 200 and "Signed in" in page.text
    assert client.get("/api/state", headers=headers).json()["kite"]["signed_in"]

    # The app restarts: the saved login is picked up without a new Kite login.
    from desk.kiteconnect import resume

    app, client = build()
    assert resume(app.state.kite, settings, "http://127.0.0.1:47800/broker/callback")
    assert client.get("/api/state", headers=headers).json()["kite"]["signed_in"]

    # Logging out ends it; a pasted redirect address signs in again.
    assert not client.post("/api/kite/logout", headers=headers).json()["signed_in"]
    pasted = client.post("/api/kite/session", headers=headers,
                         json={"url": "http://127.0.0.1:47800/broker/callback?request_token=reqtoken99&status=success"})
    assert pasted.status_code == 200 and pasted.json()["signed_in"]
