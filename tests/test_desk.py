"""TradeSetu: charges, strategy backtest, the order path, the Kite MCP client and the API."""

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
from desk.backtest import BacktestParams, run_strategy
from desk.broker import Desk, Order, OrderRejected, PaperBook
from desk.charges import order_charges
from desk.kite import KiteError, KiteMCP, resolve_transport
from desk.server import create_app
from desk.settings import Settings
from desk.strategies import STRATEGIES

pytestmark = pytest.mark.unit

FAKE = {"command": sys.executable, "args": ["-m", "desk.dev.fake_kite_mcp"]}
FAKE_READ_ONLY = {"command": sys.executable, "args": ["-m", "desk.dev.fake_kite_mcp", "--read-only"]}
OPEN = {"open": True, "time_ist": "2026-11-16 10:00:00", "note": ""}
CLOSED = {"open": False, "time_ist": "2026-11-15 10:00:00", "note": ""}


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """Each test gets its own desk home and leaves the process config as it found it."""
    monkeypatch.setenv("TRADESETU_HOME", str(tmp_path / "desk"))
    saved = copy.deepcopy(config_module._config)
    yield
    config_module._config = saved


def bars(n=600, seed=3, drift=0.0006):
    rng = np.random.default_rng(seed)
    index = pd.bdate_range("2022-01-03", periods=n)
    close = 1000 * np.exp(np.cumsum(rng.normal(drift, 0.014, n)))
    open_ = np.r_[close[0], close[:-1]] * (1 + rng.normal(0, 0.003, n))
    return pd.DataFrame({"Open": open_, "High": np.maximum(open_, close) * 1.005,
                         "Low": np.minimum(open_, close) * 0.995, "Close": close, "Volume": 1e6}, index=index)


# -- charges -----------------------------------------------------------------

def test_delivery_buy_charges_match_the_published_schedule():
    c = order_charges("BUY", 100, 1000.0, "CNC", "NSE")       # Rs 1,00,000 turnover
    assert c.brokerage == 0
    assert c.stt == pytest.approx(100.0)                      # 0.1%
    assert c.stamp_duty == pytest.approx(15.0)                # 0.015%
    assert c.exchange_txn == pytest.approx(2.97)              # 0.00297%
    assert c.sebi == pytest.approx(0.10)                      # Rs 10 a crore
    assert c.gst == pytest.approx((2.97 + 0.10) * 0.18)
    assert c.dp == 0
    assert c.total == pytest.approx(100 + 15 + 2.97 + 0.10 + 0.5526)


def test_delivery_sell_pays_stt_and_dp_but_no_stamp():
    c = order_charges("SELL", 100, 1000.0, "CNC", "BSE")
    assert c.stt == pytest.approx(100.0) and c.stamp_duty == 0 and c.dp == pytest.approx(15.34)
    assert c.exchange_txn == pytest.approx(3.75)


def test_intraday_brokerage_is_capped_and_stt_is_sell_side_only():
    small = order_charges("BUY", 10, 1000.0, "MIS")           # 0.03% of 10,000 = Rs 3
    assert small.brokerage == pytest.approx(3.0) and small.stt == 0
    assert small.stamp_duty == pytest.approx(0.30)
    big = order_charges("SELL", 1000, 1000.0, "MIS")          # 0.03% of 10,00,000 = Rs 300 -> cap
    assert big.brokerage == 20.0 and big.stt == pytest.approx(250.0) and big.dp == 0


@pytest.mark.parametrize("args", [("HOLD", 1, 1.0), ("BUY", 0, 1.0), ("BUY", 1, -1.0)])
def test_charges_reject_nonsense(args):
    with pytest.raises(ValueError):
        order_charges(*args)


# -- market ------------------------------------------------------------------

def test_session_times_are_ist_weekdays():
    ist = market.IST
    assert market.session_status(datetime(2026, 11, 16, 9, 15, tzinfo=ist))["open"]
    assert market.session_status(datetime(2026, 11, 16, 15, 29, tzinfo=ist))["open"]
    assert not market.session_status(datetime(2026, 11, 16, 15, 30, tzinfo=ist))["open"]
    assert not market.session_status(datetime(2026, 11, 16, 9, 14, tzinfo=ist))["open"]
    assert not market.session_status(datetime(2026, 11, 15, 11, 0, tzinfo=ist))["open"]   # Sunday
    # 04:00 UTC Monday is 09:30 IST
    assert market.session_status(datetime(2026, 11, 16, 4, 0, tzinfo=market.ZoneInfo("UTC")))["open"]


def test_tick_rounding_keeps_a_limit_marketable():
    assert market.round_to_tick(100.02, "BUY") == 100.05
    assert market.round_to_tick(100.02, "SELL") == 100.00
    assert market.round_to_tick(100.05, "BUY") == 100.05
    assert market.is_tick_multiple(1404.15) and not market.is_tick_multiple(1404.12)


# -- strategies and backtest ---------------------------------------------------

def test_every_strategy_runs_and_never_sees_the_future():
    data = bars()
    for key, strategy in STRATEGIES.items():
        full = strategy.positions(data)
        assert set(full.unique()) <= {0, 1}, key
        # A signal on day t must not change when later bars are removed.
        cut = strategy.positions(data.iloc[:400])
        assert full.iloc[:400].equals(cut), f"{key} looks ahead"


def test_buy_and_hold_backtest_reconciles_to_the_rupee():
    data = bars()
    params = BacktestParams(capital=1_000_000, slippage_bps=0)
    result = run_strategy(data, "buy_hold", None, params)
    trade = result["trades"][0]
    entry = data["Open"].iloc[1]                         # signal on day 0, filled at day 1's open
    assert trade["open"] and trade["entry_price"] == pytest.approx(entry, abs=0.01)
    fees = order_charges("BUY", trade["quantity"], entry, "CNC").total
    cash = 1_000_000 - trade["quantity"] * entry - fees
    assert cash >= 0 and cash < entry + fees             # could not have bought one more share
    expected = cash + trade["quantity"] * data["Close"].iloc[-1]
    assert result["metrics"]["final_value"] == pytest.approx(expected, abs=0.01)
    assert result["metrics"]["total_charges"] == pytest.approx(fees, abs=0.01)
    assert result["equity"][0][1] == 1_000_000


def test_a_flat_market_only_loses_the_charges():
    index = pd.bdate_range("2024-01-01", periods=60)
    flat = pd.DataFrame({"Open": 100.0, "High": 100.0, "Low": 100.0, "Close": 100.0, "Volume": 1.0}, index=index)
    result = run_strategy(flat, "buy_hold", None, BacktestParams(capital=100_000, slippage_bps=0))
    assert -0.5 < result["metrics"]["total_return_pct"] < 0
    assert result["metrics"]["max_drawdown_pct"] <= 0
    assert result["buy_hold"]["total_return_pct"] == 0


def test_stop_loss_exits_at_the_stop_and_gaps_fill_at_the_open():
    index = pd.bdate_range("2024-01-01", periods=40)
    price = pd.Series(100.0, index=index)
    frame = pd.DataFrame({"Open": price, "High": price, "Low": price, "Close": price, "Volume": 1.0})
    frame.iloc[10, frame.columns.get_loc("Low")] = 90.0          # intraday dip through a 5% stop
    result = run_strategy(frame, "buy_hold", None, BacktestParams(capital=100_000, slippage_bps=0, stop_loss_pct=5))
    trade = result["trades"][0]
    assert trade["exit"] == "stoploss" and trade["exit_price"] == 95.0 and len(result["trades"]) == 1

    gap = pd.DataFrame({"Open": price, "High": price, "Low": price, "Close": price, "Volume": 1.0})
    gap.iloc[10:, :4] = 80.0                                      # opens 20% lower and stays
    result = run_strategy(gap, "buy_hold", None, BacktestParams(capital=100_000, slippage_bps=0, stop_loss_pct=5))
    assert result["trades"][0]["exit_price"] == 80.0              # not the 95 it never traded at


def test_backtest_rejects_bad_input():
    data = bars()
    with pytest.raises(ValueError, match="unknown strategy"):
        run_strategy(data, "nope")
    with pytest.raises(ValueError, match="shorter"):
        run_strategy(data, "sma_cross", {"fast": 50, "slow": 20})
    with pytest.raises(ValueError, match="between"):
        run_strategy(data, "sma_cross", {"fast": -1})
    with pytest.raises(ValueError, match="unknown parameter"):
        run_strategy(data, "sma_cross", {"speed": 3})
    with pytest.raises(ValueError, match="at least 30"):
        run_strategy(data.iloc[:10], "sma_cross")
    with pytest.raises(ValueError, match="Capital"):
        run_strategy(data, "sma_cross", None, BacktestParams(capital=0))


def test_closed_trade_pnl_sums_to_the_equity_change():
    data = bars(seed=11)
    result = run_strategy(data, "sma_cross", {"fast": 10, "slow": 30}, BacktestParams(capital=500_000))
    assert result["metrics"]["trades"] >= 3
    pnl = sum(t["pnl"] for t in result["trades"])
    assert result["metrics"]["final_value"] - 500_000 == pytest.approx(pnl, abs=1.0)


# -- orders ------------------------------------------------------------------

def order(**changes):
    base = {"exchange": "NSE", "tradingsymbol": "RELIANCE", "side": "BUY", "quantity": 2,
            "product": "CNC", "order_type": "LIMIT", "price": 1404.0, "variety": "regular"}
    return {**base, **changes}


@pytest.mark.parametrize("changes,fragment", [
    ({"quantity": 0}, "whole number"), ({"quantity": 1.5}, "whole number"), ({"quantity": True}, "whole number"),
    ({"side": "HOLD"}, "side"), ({"exchange": "NCDEX"}, "exchange"), ({"price": 0}, "needs a price"),
    ({"price": 1404.12}, "tick"), ({"price": float("nan")}, "non-negative"),
    ({"tradingsymbol": "REL; DROP"}, "valid"), ({"order_type": "SL", "price": 1400}, "trigger"),
    ({"variety": "co"}, "variety"), ({"product": "NRML"}, "product"),
])
def test_malformed_orders_are_rejected(changes, fragment):
    with pytest.raises(OrderRejected, match=fragment):
        Order.parse(order(**changes))


def make_desk(tmp_path, kite=None, price=1400.0, **settings):
    cfg = Settings(tmp_path / "settings.json")
    # These tests cover orders at any hour; the trade window has its own in test_desk_trading.py.
    cfg.update({"enforce_trade_window": False, **settings})
    return Desk(cfg, kite or KiteMCP(), PaperBook(1_000_000, tmp_path / "paper.json"),
                tmp_path / "orders.jsonl", price_lookup=(lambda s: price) if price else None)


def test_paper_order_needs_its_token_and_phrase_and_fills_once(tmp_path):
    desk = make_desk(tmp_path)
    review = desk.preview(order(), "paper")
    assert review["ok"] and review["phrase"] == "BUY 2 RELIANCE" and review["charges"]["total"] > 0
    with pytest.raises(OrderRejected, match="exactly"):
        desk.place(review["token"], "BUY 3 RELIANCE")
    with pytest.raises(OrderRejected, match="expired or was already used"):   # a wrong phrase burns the token
        desk.place(review["token"], "BUY 2 RELIANCE")

    review = desk.preview(order(), "paper")
    placed = desk.place(review["token"], " buy  2 reliance ")
    assert placed["ok"] and placed["fill_price"] == 1400.0                    # at the last price, not the limit
    with pytest.raises(OrderRejected, match="already used"):
        desk.place(review["token"], "BUY 2 RELIANCE")
    book = desk.paper.snapshot()
    assert book["positions"]["NSE:RELIANCE"]["quantity"] == 2
    fees = order_charges("BUY", 2, 1400.0).total
    assert book["cash"] == pytest.approx(1_000_000 - 2800 - fees)
    assert desk.orders_today("paper") == 1 and desk.orders_today("live") == 0


def test_paper_book_refuses_unfillable_and_short_orders(tmp_path):
    desk = make_desk(tmp_path)
    assert "would not fill" in desk.preview(order(price=1300.0), "paper")["errors"][0]
    assert any("cannot sell" in e for e in desk.preview(order(side="SELL", price=1390.0), "paper")["errors"])
    assert desk.preview(order(order_type="SL", trigger_price=1390.0), "paper")["token"] is None


def test_risk_limits_block_an_order(tmp_path):
    desk = make_desk(tmp_path, max_order_value=2000.0)
    assert any("limit per order" in e for e in desk.preview(order(), "paper")["errors"])
    desk.settings.update({"max_order_value": 50_000.0, "max_orders_per_day": 1})
    review = desk.preview(order(), "paper")
    desk.place(review["token"], review["phrase"])
    assert any("limit of 1" in e for e in desk.preview(order(), "paper")["errors"])
    desk.settings.update({"max_orders_per_day": 10, "kill_switch": True})
    assert any("kill switch" in e.lower() for e in desk.preview(order(), "paper")["errors"])
    desk.settings.update({"kill_switch": False})
    assert any("above the last price" in e for e in desk.preview(order(price=1600.0), "paper")["errors"])


def test_the_kill_switch_stops_an_order_already_previewed(tmp_path):
    desk = make_desk(tmp_path)
    review = desk.preview(order(), "paper")
    desk.settings.update({"kill_switch": True})
    with pytest.raises(OrderRejected, match="kill switch"):
        desk.place(review["token"], review["phrase"])
    assert desk.paper.snapshot()["positions"] == {}


def test_live_orders_are_off_until_enabled_and_connected(tmp_path, monkeypatch):
    monkeypatch.setattr(market, "session_status", lambda *a: OPEN)
    desk = make_desk(tmp_path)
    errors = desk.preview(order(), "live")["errors"]
    assert any("switched off" in e for e in errors) and any("Not connected" in e for e in errors)


# -- Kite MCP ----------------------------------------------------------------

def test_transport_falls_back_to_the_url_when_npx_is_missing(monkeypatch):
    entry = {"command": "npx", "args": ["mcp-remote", "https://mcp.kite.trade/mcp"]}
    monkeypatch.setattr("desk.kite.shutil.which", lambda c: None)
    assert resolve_transport(entry) == {"kind": "http", "url": "https://mcp.kite.trade/mcp", "fallback_from": "npx"}
    monkeypatch.setattr("desk.kite.shutil.which", lambda c: "/usr/bin/npx")
    assert resolve_transport(entry)["kind"] == "stdio"
    assert resolve_transport({"url": "https://x/mcp"}) == {"kind": "http", "url": "https://x/mcp"}
    monkeypatch.setattr("desk.kite.shutil.which", lambda c: None)
    with pytest.raises(KiteError, match="not installed"):
        resolve_transport({"command": "kite-mcp-server"})


@pytest.fixture
def kite():
    client = KiteMCP()
    client.connect(FAKE)
    yield client
    client.disconnect()


def test_kite_session_login_and_reads(kite):
    assert kite.connected and kite.can_trade
    with pytest.raises(KiteError) as caught:
        kite.profile()
    assert caught.value.needs_login
    assert kite.login()["url"].startswith("https://kite.zerodha.com/connect/login")
    assert kite.profile()["user_id"] == "ZZ0000"
    assert kite.holdings()[0]["tradingsymbol"] == "INFY"
    assert kite.positions() == []
    assert kite.ltp(["NSE:RELIANCE", "NSE:NOPE"]) == {"NSE:RELIANCE": 1400.0}
    with pytest.raises(KiteError, match="no"):
        kite.call("delete_everything")


def test_live_order_reaches_the_broker_exactly_once(tmp_path, kite, monkeypatch):
    monkeypatch.setattr(market, "session_status", lambda *a: OPEN)
    kite.login()
    desk = make_desk(tmp_path, kite, live_enabled=True)
    review = desk.preview(order(price=1400.0), "live")
    assert review["ok"], review["errors"]
    assert review["last_price_source"] == "Kite last traded price"
    placed = desk.place(review["token"], "BUY 2 RELIANCE")
    assert placed["order_id"] == "FAKE000001"
    with pytest.raises(OrderRejected):
        desk.place(review["token"], "BUY 2 RELIANCE")
    sent = kite.orders()
    assert len(sent) == 1
    assert {k: sent[0][k] for k in ("exchange", "tradingsymbol", "transaction_type", "quantity", "product",
                                    "order_type", "price", "variety", "tag")} == {
        "exchange": "NSE", "tradingsymbol": "RELIANCE", "transaction_type": "BUY", "quantity": 2,
        "product": "CNC", "order_type": "LIMIT", "price": 1400.0, "variety": "regular", "tag": "tradesetu"}


def test_live_checks_market_hours_and_delivery_shorting(tmp_path, kite, monkeypatch):
    kite.login()
    desk = make_desk(tmp_path, kite, live_enabled=True)
    monkeypatch.setattr(market, "session_status", lambda *a: CLOSED)
    assert any("AMO" in e for e in desk.preview(order(price=1400.0), "live")["errors"])
    assert desk.preview(order(price=1400.0, variety="amo"), "live")["ok"]
    monkeypatch.setattr(market, "session_status", lambda *a: OPEN)
    assert any("not an AMO" in e for e in desk.preview(order(price=1400.0, variety="amo"), "live")["errors"])
    short = desk.preview(order(tradingsymbol="INFY", side="SELL", quantity=25, price=1500.0), "live")
    assert any("short sale" in e for e in short["errors"])
    assert desk.preview(order(tradingsymbol="INFY", side="SELL", quantity=20, price=1500.0), "live")["ok"]


def test_a_read_only_kite_server_cannot_trade(tmp_path, monkeypatch):
    monkeypatch.setattr(market, "session_status", lambda *a: OPEN)
    client = KiteMCP()
    try:
        status = client.connect(FAKE_READ_ONLY)
        assert status["connected"] and not status["can_trade"] and "place_order" in status["missing_order_tools"]
        client.login()
        desk = make_desk(tmp_path, client, live_enabled=True)
        assert any("read-only" in e for e in desk.preview(order(price=1400.0), "live")["errors"])
        with pytest.raises(KiteError, match="read-only"):
            client.place_order({})
    finally:
        client.disconnect()


def test_a_dead_server_is_reported_not_hung():
    client = KiteMCP()
    with pytest.raises(KiteError, match="could not connect|did not answer"):
        client.connect({"command": sys.executable, "args": ["-c", "raise SystemExit(1)"]}, timeout=20)
    assert not client.connected and client.status()["error"]


def test_ratings_become_sized_orders(tmp_path, kite, monkeypatch):
    monkeypatch.setattr(market, "session_status", lambda *a: OPEN)
    kite.login()
    desk = make_desk(tmp_path, kite, live_enabled=True)       # funds 2,50,000; 5% = 12,500
    buy = desk.plan("RELIANCE.NS", "Buy", "live")["order"]
    assert buy["side"] == "BUY" and buy["price"] == 1404.2 and buy["quantity"] == 8      # 12,500 // 1404.2
    assert desk.plan("RELIANCE.NS", "Overweight", "live")["order"]["quantity"] == 4
    assert desk.plan("RELIANCE.NS", "Sell", "live")["order"] is None                    # nothing held
    sell = desk.plan("INFY.NS", "Sell", "live")["order"]
    assert sell["side"] == "SELL" and sell["quantity"] == 20 and sell["price"] == 1495.5
    assert desk.plan("INFY.NS", "Underweight", "live")["order"]["quantity"] == 10
    assert desk.plan("INFY.NS", "Hold", "live")["order"] is None
    assert desk.plan("^NSEI", "Buy", "live")["order"] is None
    # A suggested order passes the same checks as a typed one.
    assert desk.preview(buy, "live")["ok"]


# -- settings and API --------------------------------------------------------

def test_settings_validate_mask_and_persist(tmp_path):
    path = tmp_path / "s.json"
    settings = Settings(path)
    assert settings.get("live_enabled") is False and settings.get("mcp")["mcpServers"]["kite"]["command"] == "npx"
    settings.update({"api_keys": {"OPENAI_API_KEY": "sk-secret-1234"}, "max_order_value": 25000})
    assert settings.public()["api_keys"]["OPENAI_API_KEY"].endswith("1234")
    assert "secret" not in str(settings.public())
    settings.update({"api_keys": {"OPENAI_API_KEY": settings.public()["api_keys"]["OPENAI_API_KEY"]}})
    assert Settings(path).get("api_keys")["OPENAI_API_KEY"] == "sk-secret-1234"       # masked echo keeps the key
    assert Settings(path).get("max_order_value") == 25000.0
    for bad in ({"nope": 1}, {"live_enabled": "yes"}, {"max_order_value": -1}, {"api_keys": {"PATH": "x"}},
                {"mcp": {"mcpServers": {"kite": {}}}}, {"analysts": []}, {"product": "NRML"},
                {"position_pct": 150}, {"max_debate_rounds": 9}):
        with pytest.raises(ValueError):
            settings.update(bad)
    assert settings.agent_config()["default_exchange"] == "NSE"


@pytest.fixture
def client(tmp_path):
    from fastapi.testclient import TestClient

    def fetch(symbol, interval="1d"):
        return bars(seed=sum(map(ord, symbol)))

    settings = Settings(tmp_path / "settings.json")
    settings.update({"mcp": {"mcpServers": {"kite": FAKE}}, "enforce_trade_window": False})
    app = create_app(settings=settings, token="t0ken", fetch=fetch)
    with TestClient(app, headers={"X-Desk-Token": "t0ken"}) as test_client:
        yield test_client
    app.state.kite.disconnect()


def test_api_needs_the_token_and_a_local_host(client):
    assert client.get("/api/state", headers={"X-Desk-Token": "wrong"}).status_code == 401
    assert client.get("/api/state", headers={"Host": "evil.example"}).status_code == 403
    page = client.get("/")
    assert page.status_code == 200 and 'content="t0ken"' in page.text
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    state = client.get("/api/state").json()
    assert state["settings"]["default_exchange"] == "NSE" and len(state["strategies"]) == len(STRATEGIES)


def test_api_strategy_backtest_and_its_errors(client):
    body = {"symbol": "reliance", "start": "2022-06-01", "end": "2024-03-01", "strategy": "ema_cross"}
    result = client.post("/api/backtest/strategy", json=body).json()
    assert result["symbol"] == "RELIANCE.NS" and result["benchmark_name"] == "Nifty 50"
    assert result["period"]["start"] >= "2022-06-01" and result["period"]["end"] <= "2024-03-01"
    assert len(result["equity"]) == result["period"]["bars"]
    for change, fragment in (({"symbol": "../../etc"}, "not a valid symbol"), ({"end": "2022-01-01"}, "after"),
                             ({"start": "01-06-2022"}, "YYYY-MM-DD"), ({"strategy": "x"}, "unknown strategy"),
                             ({"capital": "lots"}, "number"), ({"start": "2030-01-01", "end": "2031-01-01"}, "no prices")):
        response = client.post("/api/backtest/strategy", json={**body, **change})
        assert response.status_code in (400, 404) and fragment in response.json()["error"], change


def test_api_backtest_takes_rules_typed_on_the_screen_and_exit_rules(client):
    rules = {"entry": {"match": "all", "rules": [{"left": {"indicator": "close"}, "op": "crosses_above",
                                                  "right": {"indicator": "ema", "params": {"period": 20}}}]},
             "exit": {"match": "any", "rules": [{"left": {"indicator": "close"}, "op": "crosses_below",
                                                 "right": {"indicator": "ema", "params": {"period": 20}}}]}}
    body = {"symbol": "TCS", "start": "2022-06-01", "end": "2024-03-01", "rules": rules,
            "exits": {"unit": "points", "stop_loss": 20, "trail_every": 20, "trail_by": 10, "reentry": 1}}
    result = client.post("/api/backtest/strategy", json=body).json()
    assert result["strategy"]["name"] == "My rules" and result["params"]["exits"]["unit"] == "points"
    assert result["metrics"]["trades"] > 0
    assert client.get("/api/strategies").json()["custom"] == []                  # nothing saved
    bad = client.post("/api/backtest/strategy", json={**body, "exits": {"trail_every": 5}}).json()
    assert "both boxes" in bad["error"]
    state = client.get("/api/state").json()
    assert {"time", "weekday"} <= {o["key"] for o in state["indicators"]} and "is_one_of" in state["rule_ops"]


def test_api_refuses_agent_runs_without_a_key_and_oversized_grids(client, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert "API key" in client.post("/api/analyze", json={"symbol": "TCS"}).json()["error"]
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    assert "future" in client.post("/api/analyze", json={"symbol": "TCS", "date": "2999-01-01"}).json()["error"]
    grid = {"symbols": "TCS INFY RELIANCE", "start": "2026-01-01", "end": "2026-06-30", "every_n_days": 1}
    assert "above the limit" in client.post("/api/backtest/agents", json=grid).json()["error"]


def test_api_order_flow_paper_and_live(client, monkeypatch):
    monkeypatch.setattr(market, "session_status", lambda *a: OPEN)
    ticket = {"exchange": "NSE", "tradingsymbol": "TCS", "side": "BUY", "quantity": 1, "order_type": "MARKET"}
    review = client.post("/api/order/preview", json={"mode": "paper", "order": ticket}).json()
    assert review["ok"] and review["last_price_source"].startswith("Yahoo")
    placed = client.post("/api/order/place", json={"token": review["token"], "phrase": "BUY 1 TCS"})
    assert placed.status_code == 200 and placed.json()["order_id"] == "PAPER000001"
    assert client.post("/api/order/place", json={"token": review["token"], "phrase": "BUY 1 TCS"}).status_code == 422
    assert client.post("/api/order/preview", json={"mode": "paper", "order": {"side": "BUY"}}).status_code == 422

    assert client.post("/api/kite/connect").json()["can_trade"]
    assert client.get("/api/kite/account").json()["needs_login"]
    assert "kite.zerodha.com" in client.post("/api/kite/login").json()["url"]
    assert client.get("/api/kite/account").json()["profile"]["user_id"] == "ZZ0000"
    live = {**ticket, "tradingsymbol": "RELIANCE", "order_type": "LIMIT", "price": 1400.0}
    blocked = client.post("/api/order/preview", json={"mode": "live", "order": live}).json()
    assert not blocked["ok"] and blocked["token"] is None                      # live is off by default
    client.put("/api/settings", json={"live_enabled": True})
    review = client.post("/api/order/preview", json={"mode": "live", "order": live}).json()
    assert review["ok"], review["errors"]
    assert client.post("/api/order/place", json={"token": review["token"], "phrase": "BUY 1 RELIANCE"}).json()["order_id"] == "FAKE000001"
    journal = client.get("/api/order/journal").json()["orders"]
    assert [row["mode"] for row in journal] == ["live", "paper"]


def test_api_funds_for_paper_and_live(client, monkeypatch):
    monkeypatch.setattr(market, "session_status", lambda *a: OPEN)
    paper = client.get("/api/funds?mode=paper").json()
    assert paper["cash"] == paper["capital"] == paper["value"] and paper["invested"] == 0 and paper["change"] == 0
    ticket = {"exchange": "NSE", "tradingsymbol": "TCS", "side": "BUY", "quantity": 2, "order_type": "MARKET"}
    review = client.post("/api/order/preview", json={"mode": "paper", "order": ticket}).json()
    client.post("/api/order/place", json={"token": review["token"], "phrase": "BUY 2 TCS"})
    paper = client.get("/api/funds?mode=paper").json()
    assert paper["positions"] == 1 and paper["invested"] > 0 and not paper["unpriced"]
    assert 0 < paper["capital"] - paper["cash"] - paper["invested"] < 50                     # only the charges are gone
    assert paper["value"] == pytest.approx(paper["capital"] + paper["change"], abs=0.02)

    assert client.get("/api/funds?mode=live").json()["connected"] is False
    client.post("/api/kite/connect")
    assert client.get("/api/funds?mode=live").json()["needs_login"]
    client.post("/api/kite/login")
    live = client.get("/api/funds?mode=live").json()
    assert [s["segment"] for s in live["segments"]] == ["equity", "commodity"]
    assert live["segments"][0]["net"] == 250000.0 and live["segments"][0]["used"] == 0.0
    assert client.get("/api/funds?mode=both").status_code == 400


def test_an_analysis_job_runs_and_reports_its_result(client, monkeypatch):
    import time

    import tradingagents.graph.trading_graph as trading_graph

    seen = {}

    class _Graph:
        def __init__(self, analysts, config=None, callbacks=None):
            seen.update(analysts=analysts, exchange=config["default_exchange"], callbacks=callbacks)

        def propagate(self, symbol, date, portfolio=None):
            seen.update(symbol=symbol, date=date, portfolio=portfolio)
            return {"market_report": "## Trend\nUp.", "final_trade_decision": "**Rating**: Buy"}, "Buy"

        def save_reports(self, state, symbol):
            raise OSError("disk full")          # the analysis must survive this

        def run_settings(self):
            return {"llm_provider": "openai"}

    monkeypatch.setattr(trading_graph, "TradingAgentsGraph", _Graph)
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    started = client.post("/api/analyze", json={"symbol": "nse:tcs", "date": "2026-01-05", "use_portfolio": True})
    job_id = started.json()["job"]["id"]
    for _ in range(100):
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("done", "error"):
            break
        time.sleep(0.05)
    assert job["status"] == "done", job["error"]
    assert job["result"]["rating"] == "Buy" and job["result"]["symbol"] == "TCS.NS"
    assert [r["key"] for r in job["result"]["reports"]] == ["market_report", "final_trade_decision"]
    assert any("Report not saved" in e["message"] for e in job["events"])
    assert seen["symbol"] == "TCS.NS" and seen["exchange"] == "NSE" and seen["portfolio"].currency == "INR"
    assert client.get("/api/jobs/nope").status_code == 404

    class _Broken(_Graph):
        def propagate(self, *a, **k):
            raise RuntimeError("provider down")

    monkeypatch.setattr(trading_graph, "TradingAgentsGraph", _Broken)
    job_id = client.post("/api/analyze", json={"symbol": "TCS"}).json()["job"]["id"]
    for _ in range(100):
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("done", "error"):
            break
        time.sleep(0.05)
    assert job["status"] == "error" and "provider down" in job["error"]


def test_a_bse_symbol_with_no_history_uses_its_nse_twin(tmp_path):
    from desk import data

    def fetch(symbol):
        return bars().iloc[:1] if symbol.endswith(".BO") else bars()

    frame = data.history("SBIN.BO", fetch=fetch)
    assert len(frame) == 600 and "SBIN.NS" in frame.attrs["note"]
    assert "note" not in data.history("SBIN.NS", fetch=fetch).attrs
    with pytest.raises(data.NoData, match="scrip codes"):
        data.history("500325.BO", fetch=lambda s: None)


def test_npx_is_started_without_its_install_prompt(monkeypatch):
    monkeypatch.setattr("desk.kite.shutil.which", lambda c: "/usr/bin/npx")
    assert resolve_transport({"command": "npx", "args": ["mcp-remote", "https://x/mcp"]})["args"][0] == "-y"
    assert resolve_transport({"command": "npx", "args": ["--yes", "mcp-remote", "u"]})["args"][0] == "--yes"
    monkeypatch.setattr("desk.kite.shutil.which", lambda c: "/usr/bin/kite-mcp-server")
    assert resolve_transport({"command": "kite-mcp-server", "args": []})["args"] == []


def test_old_home_variable_still_works(tmp_path, monkeypatch):
    from desk import settings as desk_settings
    monkeypatch.delenv("TRADESETU_HOME", raising=False)
    monkeypatch.setenv("BHARAT_DESK_HOME", str(tmp_path / "old"))
    assert desk_settings.home() == tmp_path / "old"
    monkeypatch.setenv("TRADESETU_HOME", str(tmp_path / "new"))
    assert desk_settings.home() == tmp_path / "new"
