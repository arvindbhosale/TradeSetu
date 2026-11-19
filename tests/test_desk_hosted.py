"""The hosted desk: sign-in, sessions, charts, and algos that trade by themselves."""

import copy
import sys
from datetime import datetime

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("fastapi")
pytest.importorskip("mcp")

import tradingagents.dataflows.config as config_module
from desk import auth as auth_module, charts, market
from desk.algo import AlgoEngine, AlgoError, arm_phrase
from desk.auth import Auth, AuthNotConfigured, hash_password, verify_password
from desk.broker import Desk, PaperBook
from desk.kite import KiteMCP
from desk.server import create_app
from desk.settings import Settings
from desk.strategies import STRATEGIES

pytestmark = pytest.mark.unit

FAKE = {"command": sys.executable, "args": ["-m", "desk.dev.fake_kite_mcp"]}
PASSWORD = "correct horse battery"


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADESETU_HOME", str(tmp_path / "desk"))
    saved = copy.deepcopy(config_module._config)
    config_module.set_config({"default_exchange": "NSE"})
    yield
    config_module._config = saved


def bars(n=300, end="2026-11-16", seed=5):
    rng = np.random.default_rng(seed)
    index = pd.bdate_range(end=end, periods=n)
    close = 1000 * np.exp(np.cumsum(rng.normal(0.0004, 0.014, n)))
    open_ = np.r_[close[0], close[:-1]]
    return pd.DataFrame({"Open": open_, "High": np.maximum(open_, close) * 1.004,
                         "Low": np.minimum(open_, close) * 0.996, "Close": close, "Volume": 1e6}, index=index)


# -- auth --------------------------------------------------------------------

def test_password_hashes_verify_and_differ():
    stored = hash_password(PASSWORD)
    assert stored != hash_password(PASSWORD)                     # salted
    assert verify_password(PASSWORD, stored) and not verify_password(PASSWORD + "x", stored)
    assert not verify_password(PASSWORD, "garbage") and not verify_password(PASSWORD, "md5$a$b")


def test_the_hosted_desk_refuses_to_start_without_a_real_password():
    for env in ({}, {"DESK_ADMIN_PASSWORD": "short"}, {"DESK_ADMIN_PASSWORD_HASH": "plaintext"}):
        with pytest.raises(AuthNotConfigured):
            Auth.from_env(env)
    auth = Auth.from_env({"DESK_ADMIN_PASSWORD": PASSWORD, "DESK_ADMIN_USER": "pawan"})
    assert auth.username == "pawan" and auth.login("pawan", PASSWORD, "ip")
    hashed = Auth.from_env({"DESK_ADMIN_PASSWORD_HASH": hash_password(PASSWORD)})
    assert hashed.username == "admin" and hashed.login("admin", PASSWORD, "ip")
    assert "$" not in hash_password(PASSWORD)                    # survives Compose and .env interpolation
    legacy = hash_password(PASSWORD).replace(":", "$")
    assert Auth.from_env({"DESK_ADMIN_PASSWORD_HASH": f'"{legacy}"'}).login("admin", PASSWORD, "ip")


def test_pasted_whitespace_never_locks_the_owner_out(caplog):
    auth = Auth.from_env({"DESK_ADMIN_PASSWORD": f"  {PASSWORD}\r\n", "DESK_ADMIN_USER": " admin "})
    assert auth.login(" admin ", f"{PASSWORD} ", "ip")
    with caplog.at_level("WARNING"):
        assert auth.login("admin", "wrong-password", "ip") is None and auth.login("root", PASSWORD, "ip") is None
        Auth.from_env({"DESK_ADMIN_PASSWORD": f'"{PASSWORD}"'})
    assert "wrong password" in caplog.text and "unknown username" in caplog.text and "quote character" in caplog.text
    assert PASSWORD not in caplog.text and "wrong-password" not in caplog.text       # never logged


def test_sessions_expire_and_failed_logins_lock_out_one_client():
    now = [0.0]
    auth = Auth("admin", hash_password(PASSWORD), clock=lambda: now[0])
    session = auth.login("admin", PASSWORD, "1.1.1.1")
    assert auth.session(session["id"])["csrf"] == session["csrf"]
    assert auth.session("nope") is None and auth.session(None) is None
    now[0] += auth_module.SESSION_SECONDS + 1
    assert auth.session(session["id"]) is None

    for _ in range(auth_module.MAX_FAILURES):
        assert auth.login("admin", "wrong", "6.6.6.6") is None
    with pytest.raises(PermissionError):
        auth.login("admin", PASSWORD, "6.6.6.6")                 # locked even with the right password
    assert auth.login("admin", PASSWORD, "1.1.1.1")              # another client is not
    now[0] += auth_module.LOCK_SECONDS + 1
    assert auth.login("admin", PASSWORD, "6.6.6.6")
    assert auth.login("root", PASSWORD, "2.2.2.2") is None

    live = auth.login("admin", PASSWORD, "1.1.1.1")
    auth.logout(live["id"])
    assert auth.session(live["id"]) is None


@pytest.fixture
def hosted(tmp_path):
    from fastapi.testclient import TestClient

    settings = Settings(tmp_path / "settings.json")
    settings.update({"mcp": {"mcpServers": {"kite": FAKE}}})
    app = create_app(settings=settings, fetch=lambda s, interval="1d": bars(seed=sum(map(ord, s))),
                     auth=Auth("admin", hash_password(PASSWORD)), allowed_hosts=["desk.example.com"])
    with TestClient(app, base_url="https://desk.example.com", follow_redirects=False) as client:
        yield client
    app.state.kite.disconnect()


def sign_in(client) -> dict:
    assert client.post("/api/login", json={"username": "admin", "password": PASSWORD}).status_code == 200
    token = client.get("/").text.split('name="desk-token" content="')[1].split('"')[0]
    return {"X-Desk-Token": token}


def test_nothing_is_served_without_signing_in(hosted):
    assert hosted.get("/").status_code == 303 and hosted.get("/").headers["location"] == "/login"
    for path in ("/api/state", "/api/algos", "/api/paper", "/api/order/journal"):
        response = hosted.get(path)
        assert response.status_code == 401 and response.json()["signed_out"], path
    assert hosted.post("/api/order/place", json={}).status_code == 401
    assert hosted.put("/api/settings", json={"live_enabled": True}).status_code == 401
    assert hosted.get("/login").status_code == 200 and hosted.get("/static/app.css").status_code == 200
    assert hosted.get("/healthz").json() == {"ok": True}
    assert hosted.get("/api/state", headers={"Host": "evil.example"}).status_code == 403
    assert hosted.post("/api/login", json={"username": "admin", "password": "nope"}).status_code == 401


def test_sign_in_sets_a_locked_down_cookie_and_needs_the_request_token(hosted):
    response = hosted.post("/api/login", json={"username": "admin", "password": PASSWORD})
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie and "secure" in cookie
    assert hosted.get("/api/state").status_code == 403                    # cookie alone: no CSRF token
    assert hosted.get("/api/state", headers={"X-Desk-Token": "guess"}).status_code == 403
    headers = sign_in(hosted)
    state = hosted.get("/api/state", headers=headers)
    assert state.status_code == 200 and state.json()["hosted"] and state.json()["user"] == "admin"
    assert "max-age" in state.headers["strict-transport-security"]
    assert PASSWORD not in state.text
    assert hosted.post("/api/logout", headers=headers).status_code == 200
    assert hosted.get("/api/state", headers=headers).status_code == 401


def test_repeated_wrong_passwords_are_throttled(hosted):
    for _ in range(auth_module.MAX_FAILURES):
        assert hosted.post("/api/login", json={"username": "admin", "password": "x"}).status_code == 401
    locked = hosted.post("/api/login", json={"username": "admin", "password": PASSWORD})
    assert locked.status_code == 429 and "Too many" in locked.json()["error"]


def test_the_desktop_app_still_refuses_public_hosts(tmp_path):
    from fastapi.testclient import TestClient

    app = create_app(settings=Settings(tmp_path / "s.json"), token="t")
    client = TestClient(app, headers={"X-Desk-Token": "t"})
    assert client.get("/api/state").json()["hosted"] is False
    assert client.get("/api/state", headers={"Host": "desk.example.com"}).status_code == 403
    assert client.get("/login").status_code == 404


# -- charts ------------------------------------------------------------------

def values(line):
    return [point["value"] for point in line["points"]]


def test_chart_data_lines_up_and_matches_the_indicators():
    data = bars()
    out = charts.build(data, "1d", ["sma20", "bollinger", "supertrend", "donchian"], ["rsi", "volume", "rsi"],
                       start="2026-06-01")
    n = len(out["candles"])
    assert out["candles"][0]["time"] >= "2026-06-01" and out["candles"][-1]["time"] == "2026-11-16"
    assert [line["name"] for line in out["lines"]][:4] == ["SMA 20", "Bollinger upper", "Bollinger mid", "Bollinger lower"]
    assert all(len(line["points"]) == n for line in out["lines"][:4])     # warmed up before the window
    sma = data["Close"].rolling(20).mean().iloc[-1]
    assert values(out["lines"][0])[-1] == pytest.approx(sma, abs=0.01)
    upper, mid, lower = (values(out["lines"][i])[-1] for i in (1, 2, 3))
    assert lower < mid < upper and mid == pytest.approx(sma, abs=0.01)
    assert [p["key"] for p in out["panels"]] == ["rsi", "volume"] and out["pivots"] is None
    assert all(0 <= v <= 100 for v in values(out["panels"][0]["lines"][0]))
    assert len(out["panels"][1]["points"]) == n
    # The drawn Supertrend band is below price exactly when the strategy is long.
    band = pd.Series(values(out["lines"][4]), index=data.index[-n:])
    long = STRATEGIES["supertrend"].positions(data).iloc[-n:]
    assert ((band < data["Close"].iloc[-n:]) == (long == 1)).all()
    for bad, fragment in (((["rainbow"], []), "unknown overlay"), (([], ["astrology"]), "unknown panel"),
                          ((["vwap"], []), "intraday")):
        with pytest.raises(ValueError, match=fragment):
            charts.build(data, "1d", *bad)
    with pytest.raises(ValueError, match="No prices"):
        charts.build(data, "1d", [], [], start="2030-01-01")


def test_pivot_points_come_from_the_period_before_and_never_look_ahead():
    assert charts.pivot_levels(110.0, 90.0, 100.0) == {"R3": 130.0, "R2": 120.0, "R1": 110.0, "P": 100.0,
                                                       "S1": 90.0, "S2": 80.0, "S3": 70.0}
    data = bars()
    out = charts.build(data, "1d", ["pivots"], [], start="2026-09-01")        # the older key: monthly on a day chart
    lines = {line["name"]: {p["time"]: p["value"] for p in line["points"]} for line in out["lines"]}
    assert list(lines) == [f"Pivot {n} (monthly)" for n in ("R3", "R2", "R1", "P", "S1", "S2", "S3")]
    october = data.loc["2026-10"]
    expected = charts.pivot_levels(october["High"].max(), october["Low"].min(), october["Close"].iloc[-1])
    for name, level in expected.items():
        november = {v for t, v in lines[f"Pivot {name} (monthly)"].items() if t.startswith("2026-11")}
        assert november == {round(level, 2)}                                   # flat all month
    # November's levels are unchanged when the rest of November has not happened yet.
    early = charts.build(data.loc[:"2026-11-03"], "1d", ["pivots"], [], start="2026-09-01")
    assert early["lines"][3]["points"][-1]["value"] == lines["Pivot P (monthly)"]["2026-11-03"]
    assert out["pivots"][0]["levels"]["P"] == pytest.approx(expected["P"], abs=0.01)
    daily = charts.build(data, "1d", ["pivots_d", "pivots_w"], [], start="2026-09-01")
    last = data.iloc[-1]
    table = {t["timeframe"]: t for t in daily["pivots"]}
    assert table["daily"]["levels"]["P"] == pytest.approx((last["High"] + last["Low"] + last["Close"]) / 3, abs=0.01)
    assert "session after" in table["daily"]["for"] and "week of" in table["weekly"]["for"]
    week = data.loc["2026-11-09":"2026-11-15"]
    assert table["weekly"]["levels"]["R3"] == pytest.approx(
        charts.pivot_levels(week["High"].max(), week["Low"].min(), week["Close"].iloc[-1])["R3"], abs=0.01)


def test_chart_api_and_its_off_switch(hosted):
    headers = sign_in(hosted)
    refused = hosted.get("/api/chart?symbol=reliance&interval=1d", headers=headers)
    assert refused.status_code == 400 and "Charts use Kite's data" in refused.json()["error"]      # no silent Yahoo
    hosted.put("/api/settings", json={"chart_source": "kite_then_yahoo"}, headers=headers)
    query = "/api/chart?symbol=reliance&interval=1d&overlays=sma50,bollinger,pivots&panels=macd,rsi"
    out = hosted.get(query, headers=headers).json()
    assert out["symbol"] == "RELIANCE.NS" and [p["key"] for p in out["panels"]] == ["macd", "rsi"]
    assert len(out["lines"]) == 11 and set(out["pivots"][0]["levels"]) == {"R3", "R2", "R1", "P", "S1", "S2", "S3"}
    assert out["live_price"] is None and "open" in out["session"]
    assert out["source"] == "Yahoo (delayed)" and "Kite could not serve" in out["note"]
    assert "unknown" in hosted.get(query.replace("sma50", "zzz"), headers=headers).json()["error"]
    assert "interval" in hosted.get(query.replace("interval=1d", "interval=2d"), headers=headers).json()["error"]
    hosted.put("/api/settings", json={"charts_enabled": False}, headers=headers)
    assert "switched off" in hosted.get(query, headers=headers).json()["error"]


# -- algos -------------------------------------------------------------------

class Clock:
    def __init__(self, text):
        self.set(text)

    def set(self, text):
        self.now = datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=market.IST)

    def __call__(self):
        return self.now


def series(closes, end):
    """Flat bars at the given closes, ending on ``end``."""
    index = pd.bdate_range(end=end, periods=len(closes))
    close = pd.Series(closes, index=index, dtype=float)
    return pd.DataFrame({"Open": close, "High": close, "Low": close, "Close": close, "Volume": 1.0})


def engine(tmp_path, feed, clock, kite=None, **settings):
    cfg = Settings(tmp_path / "settings.json")
    cfg.update({"enforce_trade_window": False, **settings})     # send at once; the window is tested elsewhere
    desk = Desk(cfg, kite or KiteMCP(), PaperBook(1_000_000, tmp_path / "paper.json"), tmp_path / "orders.jsonl",
                price_lookup=lambda s: float(feed["bars"]["Close"].iloc[-1]), now=clock)
    return AlgoEngine(desk, tmp_path / "algos.json", fetch=lambda s: feed["bars"], now=clock)


DOWN = [100.0] * 30 + [90.0] * 8          # fast average below slow: flat
UP = DOWN + [120.0] * 4                   # then crosses above: long


def new_algo(eng, **changes):
    spec = {"name": "Rel cross", "symbol": "RELIANCE", "strategy": "sma_cross",
            "strategy_params": {"fast": 3, "slow": 10}, "budget": 25_000, "mode": "paper"}
    return eng.create({**spec, **changes})


def test_algo_creation_is_validated(tmp_path):
    eng = engine(tmp_path, {"bars": series(DOWN, "2026-11-16")}, Clock("2026-11-16 16:00"))
    algo = new_algo(eng)
    assert algo["symbol"] == "RELIANCE.NS" and not algo["enabled"] and algo["position"] == 0
    for change, fragment in (({"name": "Rel cross"}, "already exists"), ({"name": ""}, "name"),
                             ({"name": "x", "symbol": "NIFTY"}, "not an NSE/BSE equity"),
                             ({"name": "x", "strategy": "buy_hold"}, "nothing to automate"),
                             ({"name": "x", "strategy": "moon"}, "Unknown strategy"),
                             ({"name": "x", "budget": 10}, "Budget"), ({"name": "x", "mode": "yolo"}, "Mode"),
                             ({"name": "x", "strategy_params": {"fast": 50, "slow": 60, "z": 1}}, "unknown parameter")):
        with pytest.raises(ValueError, match=fragment):
            new_algo(eng, **change)
    assert len(AlgoEngine(eng.desk, eng.path).list()) == 1                # persisted


def test_a_paper_algo_buys_on_a_fresh_signal_and_sells_when_it_ends(tmp_path):
    clock, feed = Clock("2026-11-16 16:00"), {"bars": series(DOWN, "2026-11-16")}
    eng = engine(tmp_path, feed, clock)
    algo_id = new_algo(eng)["id"]
    eng.set_enabled(algo_id, True)
    assert eng.due() == [algo_id]
    eng.tick()
    algo = eng.get(algo_id)
    assert algo["position"] == 0 and algo["seen_flat"] and algo["last_bar"] == "2026-11-16"
    assert eng.due() == []                                                # once a day

    clock.set("2026-11-20 16:00")
    feed["bars"] = series(UP, "2026-11-20")
    eng.tick()
    algo = eng.get(algo_id)
    assert algo["position"] == 25_000 // 120.4 and algo["average_price"] == 120.0   # filled at the last price
    assert eng.desk.paper.quantity("NSE:RELIANCE") == algo["position"]
    journal = eng.desk.journal_rows()
    assert journal[0]["source"] == "algo:Rel cross" and journal[0]["side"] == "BUY"

    clock.set("2026-11-23 16:00")
    eng.tick()                                                            # no new bar: nothing happens
    assert len(eng.desk.journal_rows()) == 1 and eng.get(algo_id)["status"] == "Waiting for today's bar"

    clock.set("2026-12-04 16:00")
    feed["bars"] = series(UP + [80.0] * 10, "2026-12-04")
    eng.tick()
    assert eng.get(algo_id)["position"] == 0 and eng.desk.paper.quantity("NSE:RELIANCE") == 0
    assert [row["side"] for row in eng.desk.journal_rows()] == ["SELL", "BUY"]


def test_a_new_algo_does_not_join_a_trend_already_running(tmp_path):
    clock = Clock("2026-11-20 16:00")
    eng = engine(tmp_path, {"bars": series(UP, "2026-11-20")}, clock)
    algo_id = new_algo(eng)["id"]
    eng.set_enabled(algo_id, True)
    eng.tick()
    algo = eng.get(algo_id)
    assert algo["position"] == 0 and "fresh entry" in algo["status"] and not eng.desk.journal_rows()


def test_algos_run_only_after_the_close_on_weekdays_and_never_on_a_forming_bar(tmp_path):
    clock, feed = Clock("2026-11-20 11:00"), {"bars": series(UP, "2026-11-20")}
    eng = engine(tmp_path, feed, clock)
    algo_id = new_algo(eng)["id"]
    eng.set_enabled(algo_id, True)
    assert eng.due() == []                                                # market still open
    eng.evaluate(algo_id, force=True)                                     # "Run now" mid-session
    assert eng.get(algo_id)["last_signal"]["bar"] == "2026-11-19"         # today's bar ignored
    clock.set("2026-11-21 16:00")
    assert eng.due() == []                                                # Saturday
    clock.set("2026-11-20 15:49")
    assert eng.due() == []
    clock.set("2026-11-20 15:50")
    assert eng.due() == [algo_id]


def test_the_kill_switch_and_risk_limits_bind_algos(tmp_path):
    clock, feed = Clock("2026-11-16 16:00"), {"bars": series(DOWN, "2026-11-16")}
    eng = engine(tmp_path, feed, clock, max_order_value=5_000.0)
    algo_id = new_algo(eng)["id"]
    eng.set_enabled(algo_id, True)
    eng.tick()
    clock.set("2026-11-20 16:00")
    feed["bars"] = series(UP, "2026-11-20")
    eng.tick()                                                            # 25,000 order > 5,000 limit
    algo = eng.get(algo_id)
    assert algo["position"] == 0 and algo["status"] == "Blocked: order refused"
    assert algo["queued"]["side"] == "BUY"                                # the signal is kept, not lost
    assert eng.due() == []                                                # retries later, not in a tight loop
    eng.desk.settings.update({"kill_switch": True, "max_order_value": 50_000.0})
    assert eng.evaluate(algo_id)["status"] == "Blocked: kill switch is on"
    eng.desk.settings.update({"kill_switch": False})
    assert eng.evaluate(algo_id)["algo"]["position"] > 0


def test_a_live_algo_must_be_armed_and_reconciles_its_order(tmp_path, monkeypatch):
    kite = KiteMCP()
    kite.connect(FAKE)
    try:
        kite.login()
        clock, feed = Clock("2026-11-16 16:00"), {"bars": series(DOWN, "2026-11-16")}
        eng = engine(tmp_path, feed, clock, kite=kite)
        algo_id = new_algo(eng, mode="live")["id"]
        with pytest.raises(AlgoError, match="ARM LIVE REL CROSS"):
            eng.set_enabled(algo_id, True)
        with pytest.raises(AlgoError, match="switched off"):
            eng.set_enabled(algo_id, True, "arm live rel cross")
        eng.desk.settings.update({"live_enabled": True})
        assert arm_phrase("Rel  cross") == "ARM LIVE REL CROSS"
        eng.set_enabled(algo_id, True, "arm live rel cross")
        eng.tick()

        clock.set("2026-11-20 16:00")
        feed["bars"] = series(UP, "2026-11-20")
        monkeypatch.setattr(kite, "ltp", lambda instruments: dict.fromkeys(instruments, 120.0))
        eng.tick()
        algo = eng.get(algo_id)
        sent = kite.orders()
        assert len(sent) == 1 and sent[0]["variety"] == "amo" and sent[0]["order_type"] == "LIMIT"
        assert sent[0]["price"] == 120.4 and sent[0]["quantity"] == 207
        assert algo["position"] == 0 and algo["pending"]["order_id"] == sent[0]["order_id"]   # sent is not filled

        # Next day: the order shows as filled; only then does the algo hold shares.
        clock.set("2026-11-23 16:00")
        feed["bars"] = series(UP + [121.0], "2026-11-23")
        monkeypatch.setattr(kite, "orders", lambda: [{**sent[0], "status": "COMPLETE", "filled_quantity": 207,
                                                      "average_price": 119.5}])
        eng.tick()
        algo = eng.get(algo_id)
        assert algo["pending"] is None and algo["position"] == 207 and algo["average_price"] == 119.5

        with pytest.raises(AlgoError, match="Stop the algo"):
            eng.delete(algo_id)
        eng.set_enabled(algo_id, False)
        with pytest.raises(AlgoError, match="still holds 207"):
            eng.delete(algo_id)
        eng.forget_position(algo_id)
        eng.delete(algo_id)
        assert eng.list() == []
    finally:
        kite.disconnect()


def test_an_unfilled_live_order_leaves_the_algo_flat(tmp_path, monkeypatch):
    kite = KiteMCP()
    kite.connect(FAKE)
    try:
        kite.login()
        clock, feed = Clock("2026-11-16 16:00"), {"bars": series(DOWN, "2026-11-16")}
        eng = engine(tmp_path, feed, clock, kite=kite, live_enabled=True)
        algo_id = new_algo(eng, mode="live")["id"]
        eng.set_enabled(algo_id, True, "ARM LIVE REL CROSS")
        eng.tick()
        clock.set("2026-11-20 16:00")
        feed["bars"] = series(UP, "2026-11-20")
        eng.tick()
        order = kite.orders()[0]
        clock.set("2026-11-23 16:00")
        feed["bars"] = series(UP + [140.0], "2026-11-23")
        monkeypatch.setattr(kite, "orders", lambda: [{**order, "status": "CANCELLED", "filled_quantity": 0}])
        eng.tick()
        algo = eng.get(algo_id)
        assert algo["position"] == 0 and any("cancelled unfilled" in e["message"] for e in algo["log"])
    finally:
        kite.disconnect()


def test_a_live_algo_without_kite_is_blocked_not_crashed(tmp_path):
    clock = Clock("2026-11-16 16:00")
    eng = engine(tmp_path, {"bars": series(DOWN, "2026-11-16")}, clock, live_enabled=True)
    algo_id = new_algo(eng, mode="live")["id"]
    eng.set_enabled(algo_id, True, "ARM LIVE REL CROSS")
    eng.tick()
    assert eng.get(algo_id)["status"] == "Blocked: Kite not connected"


def test_algo_api(hosted):
    headers = sign_in(hosted)
    spec = {"name": "TCS st", "symbol": "TCS", "strategy": "supertrend", "budget": 30000, "mode": "live"}
    algo = hosted.post("/api/algos", json=spec, headers=headers).json()
    assert algo["strategy_name"] == "Supertrend" and algo["params"] == {"period": 10, "multiplier": 3.0}
    assert hosted.get(f"/api/algos/{algo['id']}/phrase", headers=headers).json()["phrase"] == "ARM LIVE TCS ST"
    refused = hosted.post(f"/api/algos/{algo['id']}/start", json={"phrase": "yes"}, headers=headers)
    assert refused.status_code == 400 and "ARM LIVE TCS ST" in refused.json()["error"]
    ran = hosted.post(f"/api/algos/{algo['id']}/run", json={}, headers=headers).json()
    assert ran["status"] == "Blocked: Kite not connected"
    assert hosted.post("/api/algos/nope/stop", headers=headers).status_code == 400
    edited = hosted.put(f"/api/algos/{algo['id']}", json={**spec, "budget": 40000}, headers=headers).json()
    assert edited["budget"] == 40000 and edited["name"] == "TCS st"
    assert hosted.put(f"/api/algos/{algo['id']}", json={**spec, "budget": 5}, headers=headers).status_code == 400
    assert hosted.put(f"/api/algos/{algo['id']}", json={**spec, "budget": 50000}).status_code == 403   # no request token
    detail = hosted.get(f"/api/algos/{algo['id']}/detail", headers=headers).json()
    assert detail["legs"] == [] and detail["orders"] == [] and detail["can_edit"]
    assert detail["totals"] == {"realized": 0, "unrealized": 0, "total": 0, "charges": 0, "held": 0, "closed": 0}
    assert hosted.delete(f"/api/algos/{algo['id']}", headers=headers).json() == {"ok": True}
    assert hosted.get("/api/algos", headers=headers).json()["algos"] == []


def test_csv_exports_need_the_request_token(hosted):
    assert hosted.get("/api/export/fills.csv?mode=paper").status_code == 401
    headers = sign_in(hosted)
    assert hosted.get("/api/export/fills.csv?mode=paper").status_code == 403                  # cookie alone
    for kind in ("journal", "fills", "tax-lots", "pnl"):
        response = hosted.get(f"/api/export/{kind}.csv?mode=paper", headers=headers)
        assert response.status_code == 200 and response.headers["content-type"].startswith("text/csv"), kind
        assert f'filename="tradesetu-{kind}-paper.csv"' in response.headers["content-disposition"]


def test_charts_come_from_kite_when_it_is_signed_in(hosted):
    headers = sign_in(hosted)
    assert hosted.post("/api/kite/connect", headers=headers).json()["connected"]
    needs_login = hosted.get("/api/chart?symbol=RELIANCE&interval=5m", headers=headers).json()
    assert "Log in to Kite" in needs_login["error"]
    hosted.post("/api/kite/login", headers=headers)
    for interval in ("5m", "1d"):
        out = hosted.get(f"/api/chart?symbol=RELIANCE&interval={interval}&overlays=sma20&panels=volume,rsi", headers=headers).json()
        assert out["source"] == "Kite" and out["note"] is None and len(out["candles"]) > 100, out.get("error")
        assert all(c["low"] <= min(c["open"], c["close"]) and c["high"] >= max(c["open"], c["close"]) for c in out["candles"])
    intraday = hosted.get("/api/chart?symbol=RELIANCE&interval=5m", headers=headers).json()
    minutes = {(c["time"] % 86400) // 60 for c in intraday["candles"]}
    assert min(minutes) == 9 * 60 + 15 and max(minutes) < 15 * 60 + 30     # IST wall time, as the chart draws it
    index = hosted.get("/api/chart?symbol=NIFTY&interval=1d", headers=headers).json()
    assert index["symbol"] == "^NSEI" and index["source"] == "Kite"   # an index maps to Kite's own name


def test_the_manual_is_served_inside_the_app(hosted):
    assert hosted.get("/manual").status_code == 303                        # behind the sign-in
    sign_in(hosted)
    page = hosted.get("/manual")
    assert page.status_code == 200 and "<h1" in page.text and 'id="4-charts"' in page.text
    assert 'src="/static/manual/img/01-sign-in.png"' in page.text and "<table>" in page.text
    import re
    for image in set(re.findall(r'src="(/static/manual/img/[^"]+)"', page.text)):
        assert hosted.get(image).status_code == 200, image


def test_an_unusable_ai_service_is_explained_before_a_run(hosted, monkeypatch):
    headers = sign_in(hosted)
    hosted.put("/api/settings", json={"llm_provider": "openai_compatible", "backend_url": ""}, headers=headers)
    assert "Endpoint URL" in hosted.post("/api/analyze", json={"symbol": "TCS"}, headers=headers).json()["error"]
    hosted.put("/api/settings", json={"backend_url": "https://your-service.example/v1"}, headers=headers)
    assert "not a real address" in hosted.post("/api/llm/test", headers=headers).json()["error"]
    hosted.put("/api/settings", json={"backend_url": "http://127.0.0.1:9/v1"}, headers=headers)
    assert "model name" in hosted.post("/api/llm/test", headers=headers).json()["error"]
    hosted.put("/api/settings", json={"quick_think_llm": "m", "deep_think_llm": "m"}, headers=headers)
    result = hosted.post("/api/llm/test", headers=headers).json()
    assert result["ok"] is False and result["endpoint"] == "http://127.0.0.1:9/v1"
    assert "could not reach" in result["quick"]["hint"] and result["quick"]["model"] == "m"


# -- sectors -------------------------------------------------------------------

def sector_board(tmp_path, quotes=None, clock=None):
    from desk.sectors import Sectors

    def fetch(symbol, interval="1d"):
        n = 320
        index = pd.bdate_range(end="2026-11-13", periods=n)
        slope = {"UP.NS": 0.002, "DOWN.NS": -0.002}.get(symbol, 0.0)
        close = pd.Series(100 * np.exp(slope * np.arange(n)), index=index)
        if symbol == "NEW.NS":
            close = close.iloc[-40:] * 0 + np.linspace(50, 60, 40)          # listed 40 days ago
        if symbol == "GONE.NS":
            return None
        return pd.DataFrame({"Open": close, "High": close, "Low": close, "Close": close, "Volume": 1.0})

    board = Sectors(path=tmp_path / "sectors.json", fetch=fetch, quotes=quotes or (lambda symbols: {}),
                    now=clock or Clock("2026-11-13 16:00"))
    return board


def test_default_sectors_are_within_the_card_limit_and_unique():
    from desk.sectors import MAX_SECTORS, _default_sectors

    sectors = _default_sectors()
    assert 40 <= len(sectors) <= MAX_SECTORS
    assert {"defence", "pharma", "banks-private", "auto-vehicles", "heat-exchangers-process-equipment"} <= set(sectors)
    for sector in sectors.values():
        assert 4 <= len(sector["stocks"]) <= 60 and all(s.endswith(".NS") for s in sector["stocks"])


def test_a_sector_index_is_the_weighted_average_of_its_stocks(tmp_path):
    board = sector_board(tmp_path)
    made = board.save_sector("Test", {"UP": 1, "DOWN": 1, "FLAT": 2})
    points = [p["value"] for p in made["points"]]
    assert points[0] == 100.0 and len(points) == 251
    up, down = np.exp(0.002) - 1, np.exp(-0.002) - 1
    assert points[1] == pytest.approx(100 * (1 + (up + down) / 4), abs=0.01)       # FLAT carries half the weight
    assert made["stocks"][0]["symbol"] == "UP.NS" and made["stocks"][-1]["symbol"] == "DOWN.NS"    # by contribution
    shares = {r["symbol"]: r["weight_pct"] for r in made["stocks"]}
    assert shares == {"UP.NS": 25.0, "DOWN.NS": 25.0, "FLAT.NS": 50.0}
    assert sum(r["contribution_1d"] for r in made["stocks"]) == pytest.approx(made["change_1d"], abs=0.01)

    only_up = board.save_sector("Test", {"UP": 5, "DOWN": 0, "FLAT": 0}, "test")
    assert only_up["trend"] == "Uptrend" and only_up["change_1y"] == pytest.approx((np.exp(0.002 * 250) - 1) * 100, abs=0.05)
    assert board.save_sector("Test", {"UP": 0, "DOWN": 3}, "test")["trend"] == "Downtrend"
    assert board.save_sector("Test", {"FLAT": 1}, "test")["trend"] == "Sideways"
    card = next(c for c in board.cards()["sectors"] if c["id"] == "test")
    assert card["trend"] == "Sideways" and card["priced"] == 1 and len(card["candles"]) == 45
    assert all(c["low"] <= min(c["open"], c["close"]) <= max(c["open"], c["close"]) <= c["high"] for c in card["candles"])


def test_a_newly_listed_or_missing_stock_does_not_break_the_index(tmp_path):
    board = sector_board(tmp_path)
    made = board.save_sector("Mixed", "FLAT NEW GONE")
    points = [p["value"] for p in made["points"]]
    assert points[100] == 100.0                                  # before NEW listed, only FLAT counts
    assert points[-1] > 100                                      # NEW's rise joins from its first day
    gone = next(r for r in made["stocks"] if r["symbol"] == "GONE.NS")
    assert gone["priced"] is False and "no price history" in gone["note"]
    assert next(c for c in board.cards()["sectors"] if c["id"] == "mixed")["priced"] == 2


def test_live_prices_move_the_last_point_only(tmp_path):
    clock = Clock("2026-11-16 11:00")                             # Monday, market open, no bar for today yet
    live = {"p": {}}
    board = sector_board(tmp_path, quotes=lambda symbols: live["p"], clock=clock)
    before = board.save_sector("Live", {"FLAT": 1, "UP": 1})
    live["p"] = {"FLAT.NS": 110.0}
    board.refresh_live()
    after = board.detail("live")
    assert after["points"][-1]["time"] == "2026-11-16" and len(after["points"]) == len(before["points"])
    assert after["change_1d"] == pytest.approx(5.0, abs=0.01)     # FLAT +10%, UP unchanged, equal weights
    assert next(r for r in after["stocks"] if r["symbol"] == "FLAT.NS")["price"] == 110.0
    assert board.cards()["live_source"] == "live"


def test_sector_edits_are_validated_and_persist(tmp_path):
    board = sector_board(tmp_path)
    for name, stocks, fragment in (("", "TCS", "name"), ("X", "", "at least one stock"), ("X", "NIFTY", "not an NSE/BSE stock"),
                                   ("X", {"TCS": -1}, "weight"), ("X", {"TCS": 0}, "above zero"), ("X", "A B/C", "valid symbol"),
                                   ("Defence", "HAL", "already exists")):
        with pytest.raises(ValueError, match=fragment):
            board.save_sector(name, stocks)
    board.save_sector("Drones", "IDEAFORGE ZENTEC")
    again = sector_board(tmp_path)
    assert "drones" in again._sectors and again._sectors["drones"]["stocks"] == {"IDEAFORGE.NS": 1.0, "ZENTEC.NS": 1.0}
    again.delete("drones")
    with pytest.raises(ValueError, match="no such sector"):
        again.detail("drones")
    again.reset()
    assert "defence" in again._sectors


def test_sector_api(hosted):
    headers = sign_in(hosted)
    board = hosted.get("/api/sectors", headers=headers).json()
    assert len(board["sectors"]) >= 40 and board["max"] == 50
    made = hosted.post("/api/sectors", json={"name": "My picks", "stocks": "TCS INFY"}, headers=headers).json()
    assert made["id"] == "my-picks" and len(made["points"]) > 200 and len(made["stocks"]) == 2
    changed = hosted.put("/api/sectors/my-picks", json={"name": "My picks", "stocks": {"TCS.NS": 3, "INFY.NS": 1}}, headers=headers).json()
    assert {r["symbol"]: r["weight_pct"] for r in changed["stocks"]} == {"TCS.NS": 75.0, "INFY.NS": 25.0}
    assert "weight" in hosted.put("/api/sectors/my-picks", json={"name": "My picks", "stocks": {"TCS.NS": "big"}}, headers=headers).json()["error"]
    assert hosted.delete("/api/sectors/my-picks", headers=headers).json() == {"ok": True}
    assert hosted.get("/api/sectors/my-picks", headers=headers).status_code == 400


def test_intraday_backtests_use_kites_longer_history_when_signed_in(hosted):
    headers = sign_in(hosted)
    body = {"symbol": "RELIANCE", "start": "2026-03-02", "end": "2026-11-13", "strategy": "ema_cross", "interval": "15m"}
    free = hosted.post("/api/backtest/strategy", json=body, headers=headers)
    assert free.status_code in (200, 400, 404)                      # the free feed has no such intraday history
    hosted.post("/api/kite/connect", headers=headers)
    hosted.post("/api/kite/login", headers=headers)
    account = hosted.get("/api/kite/account", headers=headers).json()
    assert account["market_data"]["ok"] and "working" in account["market_data"]["detail"]
    result = hosted.post("/api/backtest/strategy", json=body, headers=headers).json()
    assert "Candles from Kite" in result["data_note"], result.get("error")
    assert result["period"]["start"] == "2026-03-02" and result["period"]["end"] == "2026-11-13"
    assert result["period"]["days"] > 170 and result["period"]["bars"] == result["period"]["days"] * 25   # 25 candles a day
    assert all("09:30" <= t["entry_date"][11:] < "15:00" for t in result["trades"]) and result["trades"]
    too_long = hosted.post("/api/backtest/strategy", json={**body, "interval": "1m", "start": "2024-01-01"}, headers=headers).json()
    assert "at most 365 days" in too_long.get("data_note", "") or "at most 365 days" in too_long.get("error", "")


# -- installable app (PWA) and F&O expiries --------------------------------------

def test_the_app_is_installable_without_signing_in_to_fetch_its_shell(hosted):
    import json as jsonlib

    manifest = hosted.get("/static/manifest.webmanifest")
    assert manifest.status_code == 200
    data = jsonlib.loads(manifest.text)
    assert data["display"] == "standalone" and data["start_url"] == "/" and data["scope"] == "/"
    sizes = {(icon["sizes"], icon["purpose"]) for icon in data["icons"]}
    assert {("192x192", "any"), ("512x512", "any"), ("512x512", "maskable")} <= sizes
    for icon in data["icons"]:
        image = hosted.get(icon["src"])
        assert image.status_code == 200 and image.content[:8] == b"\x89PNG\r\n\x1a\n", icon["src"]
    worker = hosted.get("/sw.js")                                   # reachable before sign-in, scoped to the whole app
    assert worker.status_code == 200 and worker.headers["service-worker-allowed"] == "/"
    assert "javascript" in worker.headers["content-type"]
    assert '"/api/"' in worker.text and "caches" in worker.text     # it names the API as never cached
    assert hosted.get("/static/offline.html").status_code == 200
    login = hosted.get("/login")
    assert 'rel="manifest"' in login.text and "worker-src 'self'" in login.headers["content-security-policy"]
    assert hosted.get("/api/state").status_code == 401              # installing changes nothing about access


def test_fno_contracts_and_expiries_come_from_kite_and_roll_forward(tmp_path, monkeypatch):
    from desk.fno import Derivatives
    from desk.kite import KiteError

    kite = KiteMCP()
    clock = Clock("2026-11-16 10:00")
    book = Derivatives(kite, tmp_path / "fno.json", now=clock)
    assert book.stale() and book.view()["stocks"] == []
    with pytest.raises(KiteError, match="Connect Kite"):
        book.refresh()
    kite.connect({**FAKE, "env": {"FAKE_KITE_ANCHOR": "2026-11-16"}})
    try:
        view = book.refresh()                                       # no Kite login needed for the instrument list
        assert [s["name"] for s in view["stocks"]] == ["INFY", "RELIANCE", "SBIN", "TCS"]      # no MCX, no expired, no index
        reliance = next(s for s in view["stocks"] if s["name"] == "RELIANCE")
        assert reliance["lot_size"] == 500 and [e["date"] for e in reliance["expiries"]] == ["2026-11-24", "2026-12-29", "2027-01-26"]
        assert reliance["next"] == {"date": "2026-11-24", "type": "monthly", "days": 8, "weekday": "Tue"}
        nifty = next(i for i in view["indices"] if i["name"] == "NIFTY")
        assert [(e["date"], e["type"]) for e in nifty["expiries"]] == [
            ("2026-11-17", "weekly"), ("2026-11-24", "monthly"), ("2026-12-01", "weekly"), ("2026-12-29", "monthly"), ("2027-01-26", "monthly")]
        assert nifty["lot_size"] == 75 and not view["stale"]
        assert Derivatives(kite, tmp_path / "fno.json", now=clock).view()["stocks"][0]["name"] == "INFY"      # kept on disk

        clock.set("2026-11-18 09:00")                               # the weekly has expired
        rolled = book.view()
        assert next(i for i in rolled["indices"] if i["name"] == "NIFTY")["next"]["date"] == "2026-11-24"
        assert rolled["stale"]                                      # and the list knows it should be rebuilt
        book.tick()
        assert not book.view()["stale"] and book.view()["fetched"].startswith("2026-11-18")
        clock.set("2026-11-24 12:00")
        assert next(s for s in book.view()["stocks"] if s["name"] == "TCS")["next"]["days"] == 0    # expiry day itself
    finally:
        kite.disconnect()


def test_fno_api(hosted):
    headers = sign_in(hosted)
    empty = hosted.get("/api/fno", headers=headers).json()
    assert empty["connected"] is False and empty["stocks"] == []
    assert "Connect Kite" in hosted.post("/api/fno/refresh", headers=headers).json()["error"]
    hosted.post("/api/kite/connect", headers=headers)
    loaded = hosted.get("/api/fno", headers=headers).json()         # fills itself once Kite is there
    assert len(loaded["stocks"]) == 4 and loaded["indices"][0]["name"] == "NIFTY"
