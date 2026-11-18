"""Alerts, the fills ledger and P&L, the daily loss limit, two-factor sign-in, holidays, trailing stops."""

import base64
import copy
import sys
from datetime import datetime

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("fastapi")
pytest.importorskip("mcp")

import tradingagents.dataflows.config as config_module
from desk import market
from desk.algo import AlgoEngine
from desk.auth import Auth, TwoFactor, hash_password, totp
from desk.backtest import BacktestParams, run_strategy
from desk.broker import Desk, OrderRejected, PaperBook
from desk.kite import KiteMCP
from desk.notify import Notifier
from desk.pnl import Ledger
from desk.server import create_app
from desk.settings import Settings

pytestmark = pytest.mark.unit
FAKE = {"command": sys.executable, "args": ["-m", "desk.dev.fake_kite_mcp"]}
PASSWORD = "correct horse battery"


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADESETU_HOME", str(tmp_path / "desk"))
    saved, holidays = copy.deepcopy(config_module._config), set(market.HOLIDAYS)
    config_module.set_config({"default_exchange": "NSE"})
    yield
    config_module._config = saved
    market.set_holidays(holidays)


class Clock:
    def __init__(self, text):
        self.set(text)

    def set(self, text):
        self.now = datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=market.IST)

    def __call__(self):
        return self.now


def make_desk(tmp_path, clock, kite=None, **settings):
    cfg = Settings(tmp_path / "settings.json")
    if settings:
        cfg.update(settings)
    prices = {"p": 1400.0}
    sent = []
    notifier = Notifier(cfg, tmp_path / "alerts.jsonl", send=lambda token, chat, text: sent.append((token, chat, text)), now=clock)
    desk = Desk(cfg, kite or KiteMCP(), PaperBook(1_000_000, tmp_path / "paper.json"), tmp_path / "orders.jsonl",
                price_lookup=lambda s: prices["p"], now=clock, notifier=notifier)
    return desk, prices, sent


def order(**changes):
    return {"exchange": "NSE", "tradingsymbol": "RELIANCE", "side": "BUY", "quantity": 10, "product": "CNC",
            "order_type": "MARKET", "variety": "regular", **changes}


def trade(desk, **changes):
    review = desk.preview(order(**changes), "paper")
    assert review["ok"], review["errors"]
    return desk.place(review["token"], review["phrase"])


# -- alerts ------------------------------------------------------------------------

def test_alerts_are_logged_always_and_sent_to_telegram_only_when_set_up(tmp_path):
    clock = Clock("2026-11-16 10:00")
    desk, _, sent = make_desk(tmp_path, clock)
    trade(desk)
    assert sent == [] and desk.notifier.recent()[0]["title"] == "Paper BUY 10 RELIANCE filled"
    assert "Enter the bot token" in desk.notifier.test()["error"]

    desk.settings.update({"alerts_enabled": True, "telegram_chat_id": "123456789",
                          "api_keys": {"TELEGRAM_BOT_TOKEN": "111:SECRET-token"}})
    trade(desk, side="SELL")
    assert sent[-1][:2] == ("111:SECRET-token", "123456789") and "Paper SELL 10 RELIANCE filled" in sent[-1][2]
    assert desk.notifier.notify("Same", "thing") and not desk.notifier.notify("Same", "thing")     # not repeated
    assert len([m for m in sent if "Same" in m[2]]) == 1
    assert "SECRET" not in str(desk.settings.public()) and "SECRET" not in str(desk.notifier.recent())
    for bad in ("12", "my chat", "@x"):
        with pytest.raises(ValueError, match="chat id"):
            desk.settings.update({"telegram_chat_id": bad})


def test_a_failed_delivery_never_breaks_an_order_or_leaks_the_token(tmp_path):
    clock = Clock("2026-11-16 10:00")
    cfg = Settings(tmp_path / "settings.json")
    cfg.update({"alerts_enabled": True, "telegram_chat_id": "123456789", "api_keys": {"TELEGRAM_BOT_TOKEN": "111:SECRET"}})

    def broken(token, chat, text):
        raise RuntimeError(f"cannot reach https://api.telegram.org/bot{token}/sendMessage")

    notifier = Notifier(cfg, tmp_path / "alerts.jsonl", send=broken, now=clock)
    desk = Desk(cfg, KiteMCP(), PaperBook(1_000_000, tmp_path / "paper.json"), tmp_path / "orders.jsonl",
                price_lookup=lambda s: 1400.0, now=clock, notifier=notifier)
    assert trade(desk)["ok"]                                         # the order went through regardless
    assert "SECRET" not in notifier.last_error and "<token>" in notifier.last_error
    assert notifier.test() == {"ok": False, "error": notifier.last_error}


# -- ledger and P&L ------------------------------------------------------------------

def test_pnl_is_worked_out_from_fills_by_average_cost(tmp_path):
    clock = Clock("2026-11-16 10:00")
    ledger = Ledger(tmp_path / "fills.jsonl", now=clock)
    add = lambda side, qty, price, source="algo:A", day="2026-11-16", **kw: ledger.add(      # noqa: E731
        "paper", source, "NSE", "TCS", side, qty, price, charges=0.0, time_ist=f"{day} 10:00:00", **kw)
    add("BUY", 10, 100.0, day="2026-11-12")
    add("BUY", 10, 120.0, day="2026-11-13")           # average cost 110
    add("SELL", 5, 130.0, day="2026-11-13")           # +100
    add("SELL", 5, 90.0, day="2026-11-16")            # -100
    add("BUY", 4, 50.0, source="manual")              # opened today, still held
    ledger.add("live", "manual", "NSE", "TCS", "BUY", 1, 999.0, charges=0.0)       # another account
    report = ledger.report("paper", lambda instrument: 115.0)
    assert report["realized"] == 0.0 and report["trades"] == 2 and report["win_rate"] == 50.0
    assert [(p["time"], p["value"]) for p in report["curve"]] == [("2026-11-13", 100.0), ("2026-11-16", 0.0)]
    assert report["max_drawdown"] == -100.0 and report["best_day"] == 100.0 and report["worst_day"] == -100.0
    held = {(r["source"], r["quantity"]): r for r in report["open"]}
    assert held[("algo:A", 10)]["average"] == 110.0 and held[("algo:A", 10)]["unrealized"] == 50.0
    assert held[("manual", 4)]["unrealized"] == 260.0
    assert report["unrealized"] == 310.0
    assert report["today_realized"] == -100.0 and report["today_unrealized"] == 260.0      # only what opened today
    assert report["today_total"] == 160.0
    assert [s["source"] for s in report["sources"]] == ["algo:A", "manual"]
    assert report["closed"][0]["pnl"] == -100.0 and report["closed"][0]["buy_price"] == 110.0

    assert ledger.add("paper", "x", "NSE", "TCS", "BUY", 1, 10.0, trade_id="T1")
    assert not ledger.add("paper", "x", "NSE", "TCS", "BUY", 1, 10.0, trade_id="T1")        # a trade is counted once
    assert not Ledger(tmp_path / "fills.jsonl").add("paper", "x", "NSE", "TCS", "BUY", 1, 10.0, trade_id="T1")
    assert not ledger.add("paper", "x", "NSE", "TCS", "BUY", 0, 10.0)
    # A sale of shares the desk never bought is not counted as its profit.
    other = Ledger(tmp_path / "other.jsonl", now=clock)
    other.add("paper", "manual", "NSE", "INFY", "SELL", 5, 100.0)
    assert other.report("paper")["realized"] == 0.0 and other.report("paper")["trades"] == 0


def test_charges_come_out_of_the_profit(tmp_path):
    clock = Clock("2026-11-16 10:00")
    desk, prices, _ = make_desk(tmp_path, clock)
    trade(desk)
    prices["p"] = 1450.0
    trade(desk, side="SELL")
    report = desk.pnl("paper")
    from desk.charges import order_charges
    fees = order_charges("BUY", 10, 1400.0).total + order_charges("SELL", 10, 1450.0).total
    assert report["realized"] == pytest.approx(500 - fees, abs=0.02) and report["charges"] == pytest.approx(fees, abs=0.02)
    assert report["sources"][0]["source"] == "manual" and report["open"] == []


def test_live_fills_are_read_from_kites_trade_book_once(tmp_path, monkeypatch):
    clock = Clock("2026-11-16 10:00")
    kite = KiteMCP()
    kite.connect(FAKE)
    try:
        kite.login()
        desk, _, _ = make_desk(tmp_path, clock, kite=kite, live_enabled=True)
        review = desk.preview(order(order_type="LIMIT", price=1400.0, quantity=3), "live")
        desk.place(review["token"], review["phrase"])
        kite.call("place_order", {"variety": "regular", "exchange": "NSE", "tradingsymbol": "INFY", "transaction_type": "BUY",
                                  "quantity": 9, "product": "CNC", "order_type": "LIMIT", "price": 1500.0})   # placed outside the desk
        assert desk.sync_live_fills() == 1 and desk.sync_live_fills() == 0
        report = desk.pnl("live")
        assert [(r["symbol"], r["quantity"], r["average"] > 1400) for r in report["open"]] == [("RELIANCE", 3, True)]
        assert any("LIVE BUY 3 RELIANCE filled" in a["title"] for a in desk.notifier.recent())
    finally:
        kite.disconnect()


# -- kill switch and the daily loss limit ----------------------------------------------

def test_the_kill_switch_stops_buys_but_never_an_exit(tmp_path):
    clock = Clock("2026-11-16 10:00")
    desk, _, _ = make_desk(tmp_path, clock)
    trade(desk)
    desk.settings.update({"kill_switch": True})
    assert any("kill switch" in e for e in desk.preview(order(), "paper")["errors"])
    assert trade(desk, side="SELL")["ok"]                              # you can always get out
    gtt = {"exchange": "NSE", "tradingsymbol": "RELIANCE", "side": "BUY", "quantity": 1, "kind": "single", "trigger": 1300.0}
    with pytest.raises(OrderRejected, match="kill switch"):
        desk.auto_gtt(gtt, "paper", "test")


def test_the_daily_loss_limit_turns_the_kill_switch_on_once(tmp_path):
    clock = Clock("2026-11-16 10:00")
    desk, prices, sent = make_desk(tmp_path, clock, daily_loss_limit=2000.0, max_order_value=500_000.0)
    trade(desk, quantity=100)                                          # 100 @ 1400
    prices["p"] = 1385.0
    assert desk.check_loss_limit() == [] and not desk.settings.get("kill_switch")        # down 1,500 + charges
    prices["p"] = 1375.0
    assert desk.check_loss_limit() == ["paper"]                        # down 2,500
    assert desk.settings.get("kill_switch") and desk.flatten_day == "2026-11-16"
    alert = desk.notifier.recent()[0]
    assert alert["level"] == "bad" and alert["title"] == "Daily loss limit hit on the paper account" and "2,000" in alert["body"]
    desk.settings.update({"kill_switch": False})                       # the user overrides it
    assert desk.check_loss_limit() == [] and not desk.settings.get("kill_switch")        # not tripped again today
    clock.set("2026-11-17 10:00")
    assert desk.check_loss_limit() == []                               # yesterday's position is not today's loss
    desk.settings.update({"daily_loss_limit": 0.0})
    prices["p"] = 10.0
    assert desk.check_loss_limit() == []                               # 0 switches it off


def candles_for(day, closes, step=5):
    index = pd.DatetimeIndex([pd.Timestamp(day) + pd.Timedelta(hours=9, minutes=15 + step * k) for k in range(len(closes))])
    close = pd.Series(closes, index=index, dtype=float)
    return pd.DataFrame({"Open": close, "High": close, "Low": close, "Close": close, "Volume": 1000.0})


def intraday_engine(tmp_path, **settings):
    clock = Clock("2026-11-16 10:01")
    desk, prices, sent = make_desk(tmp_path, clock, **settings)
    history = pd.concat([candles_for(d, [100.0] * 75) for d in ("2026-11-12", "2026-11-13")])
    feed = {"bars": pd.concat([history, candles_for("2026-11-16", [100, 101, 102, 103, 104, 105, 106, 107, 108])])}
    engine = AlgoEngine(desk, tmp_path / "algos.json", fetch=lambda s, interval="1d": feed["bars"], now=clock)
    prices["p"] = 108.0
    return engine, desk, prices, clock, feed


def start_intraday(engine, **extra):
    algo_id = engine.create({"name": "Intra", "symbol": "RELIANCE", "strategy": "ema_cross",
                             "strategy_params": {"fast": 2, "slow": 5}, "budget": 50_000, "mode": "paper",
                             "interval": "5m", **extra})["id"]
    engine.set_enabled(algo_id, True)
    engine.tick()
    assert engine.get(algo_id)["position"] > 0
    return algo_id


def test_hitting_the_loss_limit_closes_intraday_positions_and_blocks_new_ones(tmp_path):
    engine, desk, prices, clock, _ = intraday_engine(tmp_path, daily_loss_limit=1000.0)
    algo_id = start_intraday(engine)
    held = engine.get(algo_id)["position"]
    clock.set("2026-11-16 10:02")
    prices["p"] = 105.0                                                 # about -1,380 on 461 shares
    engine._retry_at.clear()
    engine.tick()
    algo = engine.get(algo_id)
    assert desk.settings.get("kill_switch") and algo["position"] == 0 and held > 0
    assert any("daily loss limit" in e["message"] for e in algo["log"])
    assert desk.paper.quantity("NSE:RELIANCE") == 0
    clock.set("2026-11-16 10:03")
    prices["p"] = 108.0
    engine._retry_at.clear()
    engine.tick()
    assert engine.get(algo_id)["position"] == 0 and engine.get(algo_id)["status"] == "Blocked: kill switch is on"
    titles = [a["title"] for a in desk.notifier.recent()]
    assert "Daily loss limit hit on the paper account" in titles and any("is blocked" in t for t in titles)


# -- trailing stop ---------------------------------------------------------------------

def test_an_intraday_trailing_stop_follows_the_high_and_sells_on_the_pullback(tmp_path):
    engine, desk, prices, clock, _ = intraday_engine(tmp_path)
    algo_id = start_intraday(engine, trail_pct=1.0)
    for minute, price in (("10:02", 109.0), ("10:03", 112.0), ("10:04", 111.2)):      # 0.7% off the high: still in
        clock.set(f"2026-11-16 {minute}")
        prices["p"] = price
        engine._retry_at.clear()
        engine.tick()
        assert engine.get(algo_id)["position"] > 0, minute
    assert engine.get(algo_id)["peak"] == 112.0
    clock.set("2026-11-16 10:05")
    prices["p"] = 110.8                                                 # 1.07% below 112
    engine._retry_at.clear()
    engine.tick()
    algo = engine.get(algo_id)
    assert algo["position"] == 0 and any("trailing stoploss ₹110.88" in e["message"] for e in algo["log"])
    assert desk.pnl("paper")["realized"] > 0                            # it locked in a gain above the 108 entry


def flat(closes, end):
    index = pd.bdate_range(end=end, periods=len(closes))
    close = pd.Series(closes, index=index, dtype=float)
    return pd.DataFrame({"Open": close, "High": close, "Low": close, "Close": close, "Volume": 1000.0})


def test_a_daily_trailing_stop_raises_the_gtt_and_never_lowers_it(tmp_path):
    down, up = [100.0] * 30 + [90.0] * 8, [100.0] * 30 + [90.0] * 8 + [120.0] * 4
    clock = Clock("2026-11-16 16:00")
    desk, prices, _ = make_desk(tmp_path, clock, enforce_trade_window=False)
    feed = {"bars": flat(down, "2026-11-16")}
    engine = AlgoEngine(desk, tmp_path / "algos.json", fetch=lambda s, interval="1d": feed["bars"], now=clock)
    algo_id = engine.create({"name": "Trail", "symbol": "RELIANCE", "strategy": "sma_cross",
                             "strategy_params": {"fast": 3, "slow": 10}, "budget": 25_000, "mode": "paper", "trail_pct": 10})["id"]
    engine.set_enabled(algo_id, True)
    engine.tick()

    def day(date, closes, price):
        clock.set(f"{date} 16:00")
        feed["bars"] = flat(closes, date)
        prices["p"] = price
        engine._retry_at.clear()
        engine.tick()
        algo = engine.get(algo_id)
        return algo, (desk.paper.gtt(algo["protect"]) if algo["protect"] else None)

    algo, gtt = day("2026-11-20", up, 120.0)                            # buys at 120: stop 108
    assert algo["position"] > 0 and gtt["trigger"] == pytest.approx(108.0)
    algo, gtt = day("2026-11-23", up + [130.0], 130.0)                  # new high close: stop 117
    assert gtt["trigger"] == pytest.approx(117.0) and gtt["status"] == "active"
    assert len([g for g in desk.paper.gtts() if g["status"] == "active"]) == 1          # the old one was cancelled
    algo, gtt = day("2026-11-24", up + [130.0, 125.0], 125.0)           # lower close: the stop stays
    assert gtt["trigger"] == pytest.approx(117.0)
    assert any("Stoploss raised to ₹117.00" in e["message"] for e in algo["log"])


def test_a_backtest_trailing_stop_exits_below_the_running_high():
    index = pd.bdate_range("2026-01-01", periods=60)
    path = [100.0] * 5 + list(np.linspace(100, 150, 25)) + list(np.linspace(150, 110, 30))
    close = pd.Series(path, index=index)
    bars = pd.DataFrame({"Open": close, "High": close, "Low": close, "Close": close, "Volume": 1.0})
    plain = run_strategy(bars, "buy_hold", None, BacktestParams(capital=100_000, slippage_bps=0))
    trailed = run_strategy(bars, "buy_hold", None, BacktestParams(capital=100_000, slippage_bps=0, trailing_stop_pct=10))
    exit_trade = trailed["trades"][0]
    # The stop sits 10% under the 150 high, at 135. The first bar to reach it opens just below, so it fills there.
    assert exit_trade["exit"] == "trailing stoploss" and 133.0 < exit_trade["exit_price"] <= 135.0
    assert trailed["metrics"]["final_value"] > plain["metrics"]["final_value"]
    with pytest.raises(ValueError, match="Trailing stop"):
        run_strategy(bars, "buy_hold", None, BacktestParams(trailing_stop_pct=95))
    # With both set, whichever stop is higher is the one that acts.
    both = run_strategy(bars, "buy_hold", None, BacktestParams(capital=100_000, slippage_bps=0, trailing_stop_pct=10, stop_loss_pct=5))
    assert both["trades"][0]["exit"] == "trailing stoploss"


# -- holidays ----------------------------------------------------------------------------

def test_a_listed_holiday_closes_the_market_for_orders_and_algos(tmp_path):
    clock = Clock("2026-11-16 10:00")
    desk, _, _ = make_desk(tmp_path, clock)
    assert desk.preview(order(), "paper")["ok"]
    saved = desk.settings.update({"holidays": "2026-11-16\n2026-12-25, 2026-11-16"})
    assert saved["holidays"] == ["2026-11-16", "2026-12-25"]
    status = market.session_status(clock())
    assert not status["open"] and status["holiday"] and "holiday" in status["note"].lower()
    assert not desk.window_status()["entries"]
    assert any("between 09:30 and 15:00" in e for e in desk.preview(order(), "paper")["errors"])
    engine = AlgoEngine(desk, tmp_path / "algos.json", fetch=lambda s, interval="1d": flat([100.0] * 60, "2026-11-13"), now=clock)
    algo_id = engine.create({"name": "H", "symbol": "TCS", "strategy": "sma_cross", "budget": 20000, "mode": "paper"})["id"]
    engine.set_enabled(algo_id, True)
    clock.set("2026-11-16 16:00")
    assert engine.due() == []                                           # nothing runs on a holiday
    clock.set("2026-11-17 16:00")
    assert engine.due() == [algo_id]
    assert Settings(tmp_path / "settings.json").get("holidays") == ["2026-11-16", "2026-12-25"]     # and it persists
    with pytest.raises(ValueError, match="not a date"):
        desk.settings.update({"holidays": "25-12-2026"})


# -- two-factor sign-in --------------------------------------------------------------------

def test_totp_matches_the_published_test_vectors():
    secret = base64.b32encode(b"12345678901234567890").decode()
    assert totp(secret, 59) == "287082" and totp(secret, 1111111109) == "081804" and totp(secret, 1234567890) == "005924"


def test_two_factor_setup_use_and_recovery(tmp_path):
    wall = [1_800_000_000.0]
    second = TwoFactor(tmp_path / "2fa.json", wall=lambda: wall[0])
    assert not second.enabled and second.verify("anything")                # off: nothing to check
    started = second.begin("admin")
    assert started["uri"].startswith("otpauth://totp/TradeSetu:admin?secret=") and len(started["secret"]) == 32
    with pytest.raises(ValueError, match="does not match"):
        second.confirm("000000")
    assert not second.enabled
    codes = second.confirm(totp(started["secret"], wall[0]))
    assert second.enabled and len(codes) == 8 and second.recovery_left() == 8

    code = totp(started["secret"], wall[0])
    assert not second.verify(code)                                         # the code that set it up is spent
    wall[0] += 30
    code = totp(started["secret"], wall[0])
    assert second.verify(code) and not second.verify(code)                 # each code works once
    wall[0] += 60
    assert second.verify(totp(started["secret"], wall[0] - 30))            # a phone 30 s slow is fine
    assert not second.verify(totp(started["secret"], wall[0] + 300)) and not second.verify("12345") and not second.verify("")
    assert second.verify(codes[0].upper()) and not second.verify(codes[0]) and second.recovery_left() == 7
    assert TwoFactor(tmp_path / "2fa.json", wall=lambda: wall[0]).enabled  # survives a restart
    assert started["secret"] not in codes and codes[0] not in (tmp_path / "2fa.json").read_text()   # recovery codes are hashed


def test_a_lost_phone_can_be_recovered_from_the_server(tmp_path, monkeypatch):
    second = TwoFactor(tmp_path / "2fa.json")
    second.confirm(totp(second.begin("admin")["secret"]))
    monkeypatch.setenv("DESK_2FA_RESET", "1")
    assert not TwoFactor(tmp_path / "2fa.json").enabled


@pytest.fixture
def hosted(tmp_path):
    from fastapi.testclient import TestClient

    settings = Settings(tmp_path / "settings.json")
    wall = [1_800_000_000.0]
    auth = Auth("admin", hash_password(PASSWORD), two_factor=TwoFactor(tmp_path / "2fa.json", wall=lambda: wall[0]))
    app = create_app(settings=settings, auth=auth, fetch=lambda s, interval="1d": None)
    with TestClient(app, base_url="https://desk.example.com", follow_redirects=False) as client:
        client.wall = wall
        yield client
    app.state.kite.disconnect()


def sign_in(client, code=""):
    response = client.post("/api/login", json={"username": "admin", "password": PASSWORD, "code": code})
    if response.status_code != 200:
        return response
    token = client.get("/").text.split('name="desk-token" content="')[1].split('"')[0]
    return {"X-Desk-Token": token}


def test_two_factor_through_the_api(hosted):
    headers = sign_in(hosted)
    assert hosted.get("/api/2fa", headers=headers).json() == {"available": True, "enabled": False, "recovery_left": 0}
    assert "current password" in hosted.post("/api/2fa/setup", json={"password": "wrong"}, headers=headers).json()["error"]
    setup = hosted.post("/api/2fa/setup", json={"password": PASSWORD}, headers=headers).json()
    assert setup["qr_svg"].startswith("<svg") and "script" not in setup["qr_svg"].lower() and len(setup["secret"]) == 32
    assert "does not match" in hosted.post("/api/2fa/enable", json={"code": "123456"}, headers=headers).json()["error"]
    enabled = hosted.post("/api/2fa/enable", json={"code": totp(setup["secret"], hosted.wall[0])}, headers=headers).json()
    assert enabled["enabled"] and len(enabled["recovery_codes"]) == 8
    hosted.post("/api/logout", headers=headers)

    asked = sign_in(hosted)                                                 # right password, no code
    assert asked.status_code == 401 and asked.json()["needs_code"] and "6-digit code" in asked.json()["error"]
    assert hosted.get("/api/state").status_code == 401                      # and no session was given
    wrong = sign_in(hosted, "000000")
    assert wrong.status_code == 401 and "code" in wrong.json()["error"]
    hosted.wall[0] += 30
    headers = sign_in(hosted, totp(setup["secret"], hosted.wall[0]))
    assert isinstance(headers, dict) and hosted.get("/api/state", headers=headers).status_code == 200
    hosted.post("/api/logout", headers=headers)
    headers = sign_in(hosted, enabled["recovery_codes"][3])                 # a recovery code works once
    assert isinstance(headers, dict)
    assert hosted.get("/api/2fa", headers=headers).json()["recovery_left"] == 7
    assert "already on" in hosted.post("/api/2fa/setup", json={"password": PASSWORD}, headers=headers).json()["error"]
    refused = hosted.post("/api/2fa/disable", json={"password": PASSWORD, "code": "111111"}, headers=headers)
    assert refused.status_code == 400
    hosted.wall[0] += 30
    off = hosted.post("/api/2fa/disable", json={"password": PASSWORD, "code": totp(setup["secret"], hosted.wall[0])}, headers=headers)
    assert off.json() == {"enabled": False}
    hosted.post("/api/logout", headers=headers)
    assert isinstance(sign_in(hosted), dict)                                # password alone again
    titles = [a["title"] for a in hosted.app.state.desk.notifier.recent()]
    assert "Two-factor sign-in switched ON" in titles and "Two-factor sign-in switched OFF" in titles


def test_wrong_codes_count_towards_the_lockout(hosted):
    headers = sign_in(hosted)
    setup = hosted.post("/api/2fa/setup", json={"password": PASSWORD}, headers=headers).json()
    hosted.post("/api/2fa/enable", json={"code": totp(setup["secret"], hosted.wall[0])}, headers=headers)
    hosted.post("/api/logout", headers=headers)
    for _ in range(5):
        assert sign_in(hosted, "999999").status_code == 401
    hosted.wall[0] += 30
    assert sign_in(hosted, totp(setup["secret"], hosted.wall[0])).status_code == 429     # guessing codes locks you out


def test_pnl_and_alert_endpoints(hosted):
    headers = sign_in(hosted)
    report = hosted.get("/api/pnl?mode=paper", headers=headers).json()
    assert report["realized"] == 0 and report["curve"] == [] and report["loss_limit"] == 0
    assert hosted.get("/api/pnl?mode=margin", headers=headers).status_code == 400
    hosted.put("/api/settings", json={"daily_loss_limit": 5000}, headers=headers)
    assert hosted.get("/api/pnl?mode=live", headers=headers).json()["loss_limit"] == 5000
    assert hosted.get("/api/alerts", headers=headers).json()["alerts"] == []
    assert hosted.post("/api/alerts/test", headers=headers).json()["ok"] is False
    hosted.post("/api/logout", headers=headers)
    assert hosted.get("/api/pnl").status_code == 401
