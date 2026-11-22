"""Positional (swing) trades on intraday candles: the stock backtest and stock algos, next to intraday."""

import copy
import json
import sys

import pandas as pd
import pytest

pytest.importorskip("fastapi")
pytest.importorskip("mcp")

import tradingagents.dataflows.config as config_module
from desk import market
from desk.algo import AlgoEngine, AlgoError
from desk.backtest import BacktestParams, run_strategy
from desk.charges import order_charges
from desk.risk import ExitRules
from tests.test_desk_trading import Clock, candles_for, intraday, make_desk

pytestmark = pytest.mark.unit
FAKE = {"command": sys.executable, "args": ["-m", "desk.dev.fake_kite_mcp"]}


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("BHARAT_DESK_HOME", str(tmp_path / "desk"))
    saved = copy.deepcopy(config_module._config)
    config_module.set_config({"default_exchange": "NSE"})
    yield
    config_module._config = saved


def bars(rows):
    """5-minute candles from (stamp, open, high, low, close) rows."""
    frame = pd.DataFrame(rows, columns=["t", "Open", "High", "Low", "Close"]).set_index("t")
    frame.index = pd.DatetimeIndex(frame.index)
    frame["Volume"] = 1000.0
    return frame


class Always:
    """A strategy that is long from the first bar on: the engine's own rules decide the exits."""
    key, name, intraday_only = "always", "Always long", False

    def __init__(self, flat_after=None):
        self.flat_after = flat_after

    def resolve(self, params):
        return {}

    def positions(self, frame, params):
        out = pd.Series(1, index=frame.index)
        if self.flat_after is not None:
            out[out.index >= pd.Timestamp(self.flat_after)] = 0
        return out


def later(day, price, n=30):
    """``n`` flat 5-minute candles on ``day`` after its 09:15 candle."""
    start = pd.Timestamp(day) + pd.Timedelta(hours=9, minutes=20)
    return [(start + pd.Timedelta(minutes=5 * k), price, price, price, price) for k in range(n)]


def sessions(days, start="2026-11-09", per_day=4, price=100.0):
    """``per_day`` 5-minute candles a day, from 09:15, all at ``price`` unless changed."""
    index = pd.bdate_range(start, periods=days)
    rows = [(d + pd.Timedelta(hours=9, minutes=15 + 5 * k), price, price, price, price) for d in index for k in range(per_day)]
    return bars(rows)


# -- backtest --------------------------------------------------------------------------------------------------------

def test_intraday_candles_default_to_intraday_and_positional_holds_across_days():
    candles = intraday()
    before = run_strategy(candles, "ema_cross", None, BacktestParams(intraday=True, capital=200_000))
    again = run_strategy(candles, "ema_cross", None, BacktestParams(intraday=True, capital=200_000, positional=False))
    assert before["trades"] == again["trades"] and before["trade_type"] == "intraday"
    assert all(t["entry_date"][:10] == t["exit_date"][:10] for t in before["trades"])

    swing = run_strategy(candles, "ema_cross", None, BacktestParams(intraday=True, capital=200_000, positional=True,
                                                                  trade_start="99:99"))   # times are not even read
    assert swing["trade_type"] == "positional" and swing["params"]["trade_type"] == "positional"
    exits = {t["exit"] for t in swing["trades"]}
    assert "square-off" not in exits
    assert any(t["exit_date"] and t["entry_date"][:10] != t["exit_date"][:10] for t in swing["trades"])
    assert any(t["entry_date"][11:] < "09:30" or t["entry_date"][11:] >= "15:00" for t in swing["trades"])
    assert len(swing["equity"]) == 20                                     # still one point a day


def test_day_candles_are_positional_as_before():
    frame = sessions(40, per_day=1)
    frame.index = frame.index.normalize()
    out = run_strategy(frame, Always(), params=BacktestParams())
    assert out["trade_type"] == "positional"


def test_a_stoploss_the_price_gaps_past_overnight_fills_at_the_next_open():
    rows = [("2026-11-16 09:15", 100, 100, 100, 100)] + [(f"2026-11-16 {h}", 100, 101, 99.5, 100) for h in ("09:20", "09:25", "15:25")]
    rows += [("2026-11-17 09:15", 90, 92, 89, 91)] + later("2026-11-17", 91)                 # opens 10% lower
    exits = ExitRules(unit="pct", stop_loss=5, target=0)
    out = run_strategy(bars(rows), Always(), params=BacktestParams(intraday=True, positional=True, exits=exits,
                                                                   slippage_bps=0, capital=100_000))
    trade = out["trades"][0]
    assert trade["exit"] == "stoploss" and trade["exit_date"] == "2026-11-17 09:15"
    assert trade["exit_price"] == 90.0                                     # the open, not the 95 stoploss

    up = [("2026-11-16 09:15", 100, 100, 100, 100)] + [(f"2026-11-16 {h}", 100, 101, 99.5, 100) for h in ("09:20", "09:25", "15:25")]
    up += [("2026-11-17 09:15", 112, 113, 111, 112)] + later("2026-11-17", 112)
    out = run_strategy(bars(up), Always(), params=BacktestParams(intraday=True, positional=True, slippage_bps=0,
                                                                 exits=ExitRules(unit="pct", target=5)))
    assert out["trades"][0]["exit"] == "target" and out["trades"][0]["exit_price"] == 112.0   # gapped past 105


def test_positional_pays_delivery_charges_and_a_same_day_round_trip_pays_intraday():
    frame = sessions(40)
    overnight = run_strategy(frame, Always(flat_after="2026-11-12 09:30"),
                             params=BacktestParams(intraday=True, positional=True, slippage_bps=0, capital=100_000))
    trade = overnight["trades"][0]
    q, p = trade["quantity"], trade["entry_price"]
    cnc = order_charges("BUY", q, p, "CNC").total + order_charges("SELL", q, p, "CNC").total
    assert trade["entry_date"][:10] != trade["exit_date"][:10]
    assert trade["pnl"] == pytest.approx(-cnc, abs=0.05)

    same_day = run_strategy(frame, Always(flat_after="2026-11-09 09:25"),
                            params=BacktestParams(intraday=True, positional=True, slippage_bps=0, capital=100_000))
    trade = same_day["trades"][0]
    q, p = trade["quantity"], trade["entry_price"]
    mis = order_charges("BUY", q, p, "MIS").total + order_charges("SELL", q, p, "MIS").total
    assert trade["entry_date"][:10] == trade["exit_date"][:10]
    assert trade["pnl"] == pytest.approx(-mis, abs=0.05) and mis < cnc
    assert same_day["metrics"]["total_charges"] == pytest.approx(mis, abs=0.05)


def test_the_holding_limit_sells_at_the_open_of_the_given_trading_day():
    frame = sessions(15)            # Mon 9 Nov 2026 onwards, weekdays
    out = run_strategy(frame, Always(), params=BacktestParams(intraday=True, positional=True, max_hold_days=3,
                                                              slippage_bps=0, capital=100_000))
    first = out["trades"][0]
    assert first["exit"] == "held 3 days"
    assert first["entry_date"] == "2026-11-09 09:20" and first["exit_date"] == "2026-11-12 09:15"
    # Blocked after the time exit: no new buy until the strategy goes flat and buys again.
    assert len(out["trades"]) == 1
    daily = sessions(40, per_day=1)
    daily.index = daily.index.normalize()
    out = run_strategy(daily, Always(), params=BacktestParams(max_hold_days=1, slippage_bps=0))
    assert out["trades"][0]["exit"] == "held 1 day" and out["trades"][0]["exit_date"] == "2026-11-11"


@pytest.mark.parametrize("params, fragment", [
    ({"intraday": True, "max_hold_days": 2}, "needs a positional trade"),
    ({"intraday": True, "positional": True, "max_hold_days": -1}, "whole number"),
    ({"intraday": True, "positional": True, "max_hold_days": 1.5}, "whole number"),
])
def test_bad_positional_settings_are_refused(params, fragment):
    with pytest.raises(ValueError, match=fragment):
        BacktestParams(**params).validate()


def test_the_backtest_api_takes_the_trade_type(tmp_path):
    from fastapi.testclient import TestClient

    from desk.server import create_app
    from desk.settings import Settings
    from tests.test_desk_trading import daily

    candles = intraday(days=10, end="2026-11-13")

    def fetch(symbol, interval="1d"):
        return daily(800) if interval == "1d" else candles

    app = create_app(settings=Settings(tmp_path / "settings.json"), token="t0ken", fetch=fetch)
    with TestClient(app, headers={"X-Desk-Token": "t0ken"}) as client:
        base = {"symbol": "RELIANCE", "start": "2026-10-20", "end": "2026-11-14", "strategy": "ema_cross", "capital": 200_000}
        old = client.post("/api/backtest/strategy", json={**base, "interval": "5m"})
        assert old.status_code == 200, old.text
        assert old.json()["trade_type"] == "intraday"
        swing = client.post("/api/backtest/strategy", json={**base, "interval": "5m", "trade_type": "positional",
                                                            "max_hold_days": 2}).json()
        assert swing["trade_type"] == "positional" and swing["params"]["max_hold_days"] == 2
        assert "square-off" not in {t["exit"] for t in swing["trades"]}
        refused = client.post("/api/backtest/strategy", json={**base, "interval": "1d", "trade_type": "intraday"})
        assert refused.status_code == 400 and "Day candles are positional" in refused.text


# -- algos -----------------------------------------------------------------------------------------------------------

def engine_for(tmp_path, feed, clock, **settings):
    desk, prices = make_desk(tmp_path, clock, price={"p": 100.0}, **settings)
    return AlgoEngine(desk, tmp_path / "algos.json", fetch=lambda s, interval="1d": feed["bars"], now=clock), prices


SPEC = {"name": "Swing", "symbol": "RELIANCE", "strategy": "ema_cross", "strategy_params": {"fast": 2, "slow": 5},
        "budget": 50_000, "mode": "paper", "interval": "5m", "trade_type": "positional",
        "exits": {"unit": "pct", "stop_loss": 3, "target": 10}}


def test_a_positional_algo_buys_cnc_protects_with_a_gtt_and_carries_overnight(tmp_path):
    history = pd.concat([candles_for(d, [100.0] * 75) for d in ("2026-11-12", "2026-11-13")])
    feed, clock = {"bars": history}, Clock("2026-11-16 09:16")
    engine, prices = engine_for(tmp_path, feed, clock)
    algo_id = engine.create({**SPEC, "max_hold_days": 2})["id"]
    algo = engine.get(algo_id)
    assert algo["product"] == "CNC" and algo["trade_type"] == "positional"
    engine.set_enabled(algo_id, True)

    def at(day, moment, closes, price):
        clock.set(f"{day} {moment}")
        feed["bars"] = pd.concat([history, candles_for(day, closes)])
        prices["p"] = price
        engine._retry_at.clear()
        engine.tick()
        return engine.get(algo_id)

    algo = at("2026-11-16", "09:36", [100, 104, 105, 106], 106.0)
    assert algo["position"] > 0 and algo["entry_day"] == "2026-11-16"
    assert engine.desk.journal_rows()[0]["product"] == "CNC"
    gtt = engine.desk.paper.gtt(algo["protect"])
    assert gtt["kind"] == "oco" and gtt["stop"] == pytest.approx(102.8, abs=0.05)
    held = algo["position"]

    closes = [100, 104, 105, 106] + [107.0] * 70
    algo = at("2026-11-16", "15:25", closes, 107.0)                   # past the square-off time: still held
    assert algo["position"] == held and not any("square-off" in e["message"] for e in algo["log"])
    clock.set("2026-11-16 20:00")
    algo = engine.evaluate(algo_id)["algo"]
    assert algo["position"] == held and "market closed" in algo["status"]
    assert engine.due() == []                                         # nothing to do overnight

    # A restart (a new engine on the same file) keeps the position, the GTT and the entry day.
    engine, prices = AlgoEngine(engine.desk, tmp_path / "algos.json", fetch=engine.fetch, now=clock), prices
    algo = at("2026-11-17", "09:31", [107.0] * 4, 107.0)
    assert algo["position"] == held and algo["protect"] and algo["entry_day"] == "2026-11-16"

    algo = at("2026-11-18", "09:21", [107.0] * 2, 107.5)              # the second trading day after the buy
    assert algo["position"] == 0 and any("held 2 days" in e["message"] for e in algo["log"])
    assert algo["protect"] is None and engine.desk.paper.quantity("NSE:RELIANCE") == 0


def test_a_positional_algo_is_not_sold_by_the_daily_loss_limit_and_its_gtt_can_close_it(tmp_path):
    history = pd.concat([candles_for(d, [100.0] * 75) for d in ("2026-11-12", "2026-11-13")])
    feed, clock = {"bars": history}, Clock("2026-11-16 09:36")
    engine, prices = engine_for(tmp_path, feed, clock)
    algo_id = engine.create(SPEC)["id"]
    engine.set_enabled(algo_id, True)
    feed["bars"] = pd.concat([history, candles_for("2026-11-16", [100, 104, 105, 106])])
    prices["p"] = 106.0
    engine.tick()
    assert engine.get(algo_id)["position"] > 0
    engine.desk.flatten_day = "2026-11-16"
    clock.set("2026-11-16 10:00")
    engine._retry_at.clear()
    engine.tick()
    assert engine.get(algo_id)["position"] > 0

    clock.set("2026-11-17 09:16")                                     # opens below the stoploss: the GTT fires
    prices["p"] = 101.0
    engine._retry_at.clear()
    engine.tick()
    algo = engine.get(algo_id)
    assert algo["position"] == 0 and algo["protect"] is None and algo["blocked"]
    assert engine.desk.paper.quantity("NSE:RELIANCE") == 0


def test_positional_algo_settings_are_checked_and_old_algos_keep_their_type(tmp_path):
    clock = Clock("2026-11-16 10:00")
    engine, _ = engine_for(tmp_path, {"bars": None}, clock)
    with pytest.raises(AlgoError, match="Day candles are positional"):
        engine.create({**SPEC, "interval": "1d", "trade_type": "intraday"})
    with pytest.raises(AlgoError, match="needs a positional algo"):
        engine.create({**SPEC, "trade_type": "intraday", "max_hold_days": 3})
    with pytest.raises(AlgoError, match="whole number"):
        engine.create({**SPEC, "max_hold_days": 2.5})
    assert engine.create({**SPEC, "name": "Old5", "trade_type": None})["product"] == "MIS"     # left out: intraday
    assert engine.create({**SPEC, "name": "Day", "interval": "1d", "trade_type": None})["trade_type"] == "positional"

    stored = json.loads((tmp_path / "algos.json").read_text())
    for algo in stored:                      # as saved before the trade type existed
        algo.pop("trade_type"), algo.pop("max_hold_days")
    (tmp_path / "algos.json").write_text(json.dumps(stored))
    reloaded = AlgoEngine(engine.desk, tmp_path / "algos.json", now=clock)
    types = {a["name"]: a["trade_type"] for a in reloaded.list()}
    assert types == {"Old5": "intraday", "Day": "positional"}


def test_a_live_delivery_sell_counts_shares_bought_today(tmp_path):
    clock = Clock("2026-11-16 11:00")
    desk, _ = make_desk(tmp_path, clock)

    class Kite:
        def holdings(self):
            return [{"tradingsymbol": "RELIANCE", "quantity": 5, "t1_quantity": 2}]

        def positions(self):
            return [{"tradingsymbol": "RELIANCE", "exchange": "NSE", "product": "CNC", "quantity": 10,
                     "overnight_quantity": 0, "day_buy_quantity": 10, "day_sell_quantity": 0},
                    {"tradingsymbol": "RELIANCE", "exchange": "NSE", "product": "MIS", "quantity": 50,
                     "day_buy_quantity": 50, "day_sell_quantity": 0}]

    desk.kite = Kite()
    from desk.broker import Order
    sell = Order.parse({"exchange": "NSE", "tradingsymbol": "RELIANCE", "side": "SELL", "quantity": 17, "product": "CNC",
                        "order_type": "LIMIT", "price": 100.0, "variety": "regular"})
    assert desk.live_holding(sell) == 17                              # 5 + 2 held, 10 bought today; MIS not counted

    class Broken(Kite):
        def positions(self):
            from desk.kite import KiteError
            raise KiteError("down")

    desk.kite = Broken()
    assert desk.live_holding(sell) == 7                               # holdings alone: never more than is there
    assert market.is_trading_day(clock())
