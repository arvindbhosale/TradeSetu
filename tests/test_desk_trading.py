"""Indicators, custom strategies, intraday backtests, the trade window, GTTs, intraday algos, the scanner."""

import copy
import sys
from datetime import datetime

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("fastapi")
pytest.importorskip("mcp")

import tradingagents.dataflows.config as config_module
from desk import custom, indicators as ind, market, scanner as scanner_module
from desk.algo import AlgoEngine, AlgoError
from desk.backtest import BacktestParams, run_strategy
from desk.broker import Desk, Gtt, OrderRejected, PaperBook
from desk.custom import CustomStrategies
from desk.kite import KiteMCP
from desk.scanner import Scanner, find_setup
from desk.settings import Settings
from desk.strategies import STRATEGIES

pytestmark = pytest.mark.unit
FAKE = {"command": sys.executable, "args": ["-m", "desk.dev.fake_kite_mcp"]}


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADESETU_HOME", str(tmp_path / "desk"))
    saved = copy.deepcopy(config_module._config)
    config_module.set_config({"default_exchange": "NSE"})
    yield
    config_module._config = saved


class Clock:
    def __init__(self, text):
        self.set(text)

    def set(self, text):
        self.now = datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=market.IST)

    def __call__(self):
        return self.now


def daily(n=500, seed=3):
    rng = np.random.default_rng(seed)
    index = pd.bdate_range("2024-01-01", periods=n)
    close = 1000 * np.exp(np.cumsum(rng.normal(0.0005, 0.015, n)))
    open_ = np.r_[close[0], close[:-1]] * (1 + rng.normal(0, 0.003, n))
    return pd.DataFrame({"Open": open_, "High": np.maximum(open_, close) * 1.005, "Low": np.minimum(open_, close) * 0.995,
                         "Close": close, "Volume": rng.integers(1e5, 5e6, n).astype(float)}, index=index)


def intraday(days=20, step=5, seed=4, end="2026-11-16"):
    rng = np.random.default_rng(seed)
    sessions = pd.bdate_range(end=end, periods=days)
    index = pd.DatetimeIndex([d + pd.Timedelta(hours=9, minutes=15 + step * k) for d in sessions for k in range(375 // step)])
    n = len(index)
    close = 1000 * np.exp(np.cumsum(rng.normal(0, 0.0015, n)))
    open_ = np.r_[close[0], close[:-1]]
    return pd.DataFrame({"Open": open_, "High": np.maximum(open_, close) * 1.0008, "Low": np.minimum(open_, close) * 0.9992,
                         "Close": close, "Volume": rng.integers(1e3, 5e4, n).astype(float)}, index=index)


def flat(closes, end, freq_days=True):
    index = pd.bdate_range(end=end, periods=len(closes))
    close = pd.Series(closes, index=index, dtype=float)
    return pd.DataFrame({"Open": close, "High": close, "Low": close, "Close": close, "Volume": 1000.0})


def make_desk(tmp_path, clock, kite=None, price=None, **settings):
    cfg = Settings(tmp_path / "settings.json")
    if settings:
        cfg.update(settings)
    prices = price if isinstance(price, dict) else {"p": price or 1400.0}
    desk = Desk(cfg, kite or KiteMCP(), PaperBook(1_000_000, tmp_path / "paper.json"), tmp_path / "orders.jsonl",
                price_lookup=lambda s: prices["p"], now=clock)
    return desk, prices


def order(**changes):
    return {"exchange": "NSE", "tradingsymbol": "RELIANCE", "side": "BUY", "quantity": 2, "product": "CNC",
            "order_type": "LIMIT", "price": 1404.0, "variety": "regular", **changes}


# -- indicators and strategies ---------------------------------------------------

def test_no_indicator_or_strategy_uses_future_bars():
    bars, candles = daily(), intraday()
    for key in ind.OPERANDS:
        source = candles if key == "vwap" else bars
        full, cut = ind.compute(source, key), ind.compute(source.iloc[:300], key)
        assert np.allclose(full.iloc[:300].fillna(-1e9), cut.fillna(-1e9)), f"{key} looks ahead"
    for key, strategy in STRATEGIES.items():
        source = candles if strategy.intraday_only else bars
        full, cut = strategy.positions(source), strategy.positions(source.iloc[:300])
        assert full.iloc[:300].equals(cut), f"{key} looks ahead"
        assert set(full.unique()) <= {0, 1}


def test_indicators_match_their_definitions():
    bars = daily()
    close = bars["Close"]
    assert ind.sma(close, 20).iloc[-1] == pytest.approx(close.iloc[-20:].mean())
    upper, mid, lower = ind.bollinger(close, 20, 2.0)
    assert upper.iloc[-1] - mid.iloc[-1] == pytest.approx(2 * close.iloc[-20:].std(ddof=0))
    assert ind.rsi(pd.Series(np.arange(1.0, 60.0)), 14).iloc[-1] == 100.0          # only gains
    assert ind.rsi(pd.Series(np.arange(60.0, 1.0, -1)), 14).iloc[-1] == pytest.approx(0.0)
    line, signal, hist = ind.macd(close)
    assert (hist.dropna() == (line - signal).dropna()).all()
    sar, trend = ind.parabolic_sar(bars)
    assert ((sar < bars["Low"]) == (trend == 1)).iloc[5:].all()                     # SAR sits on the far side of price
    adx, plus_di, minus_di = ind.adx(bars)
    assert adx.dropna().between(0, 100).all() and plus_di.dropna().ge(0).all()
    k, d = ind.stochastic(bars)
    assert k.dropna().between(0, 100).all()
    ha = ind.heikin_ashi(bars)
    assert ha["Close"].iloc[5] == pytest.approx(bars.iloc[5][["Open", "High", "Low", "Close"]].mean())
    assert (ha["High"] >= ha[["Open", "Close"]].max(axis=1)).all()
    # VWAP restarts each day: the first candle's VWAP is its own typical price.
    candles = intraday()
    first = candles.groupby(candles.index.normalize()).head(1)
    assert np.allclose(ind.vwap(candles).loc[first.index], (first["High"] + first["Low"] + first["Close"]) / 3)


RULES = {
    "name": "RSI dip in an uptrend",
    "entry": {"match": "all", "rules": [
        {"left": {"indicator": "rsi", "params": {"period": 14}}, "op": "<", "right": {"value": 45}},
        {"left": {"indicator": "close"}, "op": ">", "right": {"indicator": "sma", "params": {"period": 50}}}]},
    "exit": {"match": "any", "rules": [
        {"left": {"indicator": "rsi"}, "op": ">", "right": {"value": 65}},
        {"left": {"indicator": "close"}, "op": "crosses_below", "right": {"indicator": "supertrend"}}]},
}


def test_a_custom_strategy_combines_indicators_exactly_as_written(tmp_path):
    bars = daily()
    spec = custom.validate(RULES)
    position = custom.positions(spec, bars)
    rsi, close, sma = ind.rsi(bars["Close"], 14), bars["Close"], ind.sma(bars["Close"], 50)
    entry = (rsi < 45) & (close > sma)
    band = ind.supertrend(bars, 10, 3.0)[0]
    crossed = (close < band) & (close.shift(1) >= band.shift(1))
    leave = (rsi > 65) | crossed
    assert position.sum() > 0
    days_in = position[position == 1].index
    assert (position.diff() == 1).sum() >= 2                                        # it trades more than once
    for day in position.index[(position.diff() == 1)]:
        assert entry.loc[day] and not leave.loc[day]                                # every entry is a rule pass
    for day in position.index[(position.diff() == -1)]:
        assert leave.loc[day]                                                       # every exit is an exit rule
    assert not (leave.loc[days_in]).any()                                           # never held through an exit
    any_entry = custom.validate({**RULES, "entry": {**RULES["entry"], "match": "any"}})
    assert custom.positions(any_entry, bars).sum() > position.sum()                 # OR is looser than AND

    store = CustomStrategies(tmp_path / "c.json")
    saved = store.save(RULES)
    assert saved["key"].startswith("custom:") and saved["entry_text"] == "RSI(14) is below 45 AND Close price is above SMA(50)"
    strategy = CustomStrategies(tmp_path / "c.json").strategy(saved["key"])        # survives a restart
    assert strategy.positions(bars).equals(position)
    result = run_strategy(bars, strategy, None, BacktestParams())
    assert result["strategy"]["name"] == "RSI dip in an uptrend" and result["metrics"]["trades"] >= 2
    with pytest.raises(ValueError, match="already exists"):
        store.save(RULES)
    store.save({**RULES, "name": "Renamed"}, saved["id"])
    assert [s["name"] for s in store.list()] == ["Renamed"]
    store.delete(saved["id"])
    with pytest.raises(ValueError, match="unknown strategy"):
        store.strategy(saved["key"])


@pytest.mark.parametrize("change,fragment", [
    ({"name": ""}, "name"), ({"entry": {"match": "all", "rules": []}}, "at least one entry"),
    ({"exit": {"match": "sometimes", "rules": RULES["exit"]["rules"]}}, "match"),
    ({"entry": {"match": "all", "rules": [{"left": {"indicator": "moon"}, "op": ">", "right": {"value": 1}}]}}, "choose an indicator"),
    ({"entry": {"match": "all", "rules": [{"left": {"indicator": "rsi"}, "op": "~", "right": {"value": 1}}]}}, "comparison"),
    ({"entry": {"match": "all", "rules": [{"left": {"value": 5}, "op": ">", "right": {"value": 1}}]}}, "left side"),
    ({"entry": {"match": "all", "rules": [{"left": {"indicator": "rsi", "params": {"period": 9999}}, "op": ">", "right": {"value": 1}}]}}, "between"),
    ({"entry": {"match": "all", "rules": [{"left": {"indicator": "rsi"}, "op": ">", "right": {"value": "high"}}]}}, "number"),
    ({"entry": {"match": "all", "rules": RULES["entry"]["rules"] * 5}}, "at most"),
])
def test_bad_custom_strategies_are_rejected(change, fragment):
    with pytest.raises(ValueError, match=fragment):
        custom.validate({**RULES, **change})


def test_crossing_rules_fire_only_on_the_bar_of_the_cross():
    close = pd.Series([1, 2, 3, 4, 5, 4, 3, 2, 3, 4, 5.0], index=pd.bdate_range("2026-01-01", periods=11))
    bars = pd.DataFrame({"Open": close, "High": close, "Low": close, "Close": close, "Volume": 1.0})
    spec = custom.validate({"name": "x", "entry": {"match": "all", "rules": [
        {"left": {"indicator": "close"}, "op": "crosses_above", "right": {"value": 3.5}}]},
        "exit": {"match": "all", "rules": [{"left": {"indicator": "close"}, "op": "crosses_below", "right": {"value": 3.5}}]}})
    assert custom.positions(spec, bars).tolist() == [0, 0, 0, 1, 1, 1, 0, 0, 0, 1, 1]


# -- intraday backtest -------------------------------------------------------------

def test_an_intraday_backtest_trades_only_inside_the_window_and_squares_off():
    candles = intraday()
    for key in ("orb", "vwap_trend", "ema_cross", "supertrend"):
        result = run_strategy(candles, key, None, BacktestParams(intraday=True, capital=200_000))
        trades = result["trades"]
        assert trades, key
        for trade in trades:
            entry, leave = pd.Timestamp(trade["entry_date"]), pd.Timestamp(trade["exit_date"])
            assert entry.strftime("%H:%M") >= "09:30" and entry.strftime("%H:%M") < "15:00", (key, trade)
            assert leave.strftime("%H:%M") <= "15:00" and entry.date() == leave.date(), (key, trade)
        assert "square-off" in {t["exit"] for t in trades} or key == "orb"
        assert len(result["equity"]) == 20                                  # one point a day
        pnl = sum(t["pnl"] for t in trades)
        assert result["metrics"]["final_value"] - 200_000 == pytest.approx(pnl, abs=1.0)
    late = run_strategy(candles, "ema_cross", None, BacktestParams(intraday=True, trade_start="14:00", square_off="15:20"))
    assert all(t["entry_date"][11:] >= "14:00" for t in late["trades"])
    with pytest.raises(ValueError, match="needs intraday"):
        run_strategy(daily(), "orb")
    with pytest.raises(ValueError, match="Trade window"):
        run_strategy(candles, "ema_cross", None, BacktestParams(intraday=True, trade_start="15:10"))


def test_intraday_trades_pay_intraday_charges():
    candles = intraday()
    result = run_strategy(candles, "ema_cross", None, BacktestParams(intraday=True, capital=200_000, slippage_bps=0))
    trade = result["trades"][-1]
    from desk.charges import order_charges
    gross = trade["quantity"] * (trade["exit_price"] - trade["entry_price"])
    fees = (order_charges("BUY", trade["quantity"], trade["entry_price"], "MIS").total
            + order_charges("SELL", trade["quantity"], trade["exit_price"], "MIS").total)
    assert trade["pnl"] == pytest.approx(gross - fees, abs=3.0)      # prices in the trade list are rounded to paise


# -- trade window --------------------------------------------------------------------

def test_limits_never_block_an_exit(tmp_path):
    clock = Clock("2026-11-16 10:00")
    desk, prices = make_desk(tmp_path, clock, max_orders_per_day=1)
    review = desk.preview(order(quantity=30), "paper")              # 42,000: inside the 50,000 limit
    desk.place(review["token"], review["phrase"])
    prices["p"] = 2000.0                                            # the holding is now worth 60,000
    assert any("limit of 1" in e for e in desk.preview(order(price=2010.0), "paper")["errors"])
    sell = desk.preview(order(side="SELL", quantity=30, price=1990.0), "paper")
    assert sell["ok"], sell["errors"]                               # over both limits, still sellable


def test_new_buys_are_taken_only_inside_the_trade_window(tmp_path):
    clock = Clock("2026-11-16 09:20")                       # Monday, market open, before 09:30
    desk, _ = make_desk(tmp_path, clock)
    assert any("between 09:30 and 15:00" in e for e in desk.preview(order(), "paper")["errors"])
    for moment, ok in (("2026-11-16 09:30", True), ("2026-11-16 14:59", True), ("2026-11-16 15:00", False),
                       ("2026-11-15 11:00", False)):     # Sunday
        clock.set(moment)
        assert desk.preview(order(), "paper")["ok"] is ok, moment
    # Selling what you hold is allowed until the close, but not after it.
    clock.set("2026-11-16 10:00")
    review = desk.preview(order(), "paper")
    desk.place(review["token"], review["phrase"])
    clock.set("2026-11-16 15:10")
    assert desk.preview(order(side="SELL", price=1396.0), "paper")["ok"]
    clock.set("2026-11-16 16:00")
    assert any("market is closed" in e for e in desk.preview(order(side="SELL", price=1396.0), "paper")["errors"])
    desk.settings.update({"enforce_trade_window": False})
    assert desk.preview(order(), "paper")["ok"]
    desk.settings.update({"enforce_trade_window": True, "trade_start": "10:00", "trade_end": "11:00"})
    clock.set("2026-11-17 09:45")
    assert not desk.preview(order(), "paper")["ok"]
    for bad in ({"trade_start": "9:30"}, {"trade_end": "16:00"}, {"square_off": "noon"}):
        with pytest.raises(ValueError):
            desk.settings.update(bad)


# -- GTT -----------------------------------------------------------------------------

def gtt(**changes):
    return {"exchange": "NSE", "tradingsymbol": "RELIANCE", "side": "BUY", "quantity": 5, "kind": "single",
            "trigger": 1350.0, **changes}


@pytest.mark.parametrize("change,fragment", [
    ({"quantity": 0}, "whole number"), ({"trigger": 0}, "trigger price"), ({"side": "HOLD"}, "side"),
    ({"kind": "oco", "side": "BUY", "target": 1500, "stop": 1300}, "is a SELL"),
    ({"kind": "oco", "side": "SELL", "target": 1300, "stop": 1500}, "target above"),
    ({"kind": "bracket"}, "kind"), ({"tradingsymbol": "A B"}, "valid"),
])
def test_malformed_gtts_are_rejected(change, fragment):
    with pytest.raises(OrderRejected, match=fragment):
        Gtt.parse(gtt(**change))


def test_a_paper_gtt_buy_waits_for_its_trigger_then_an_oco_exits(tmp_path):
    clock = Clock("2026-11-16 10:00")
    desk, prices = make_desk(tmp_path, clock)
    assert any("within 0.25%" in e for e in desk.gtt_preview(gtt(trigger=1401.0), "paper")["errors"])
    review = desk.gtt_preview(gtt(), "paper")
    assert review["ok"] and review["phrase"] == "GTT BUY 5 RELIANCE"
    with pytest.raises(OrderRejected, match="exactly"):
        desk.place(review["token"], "BUY 5 RELIANCE")
    placed = desk.place(desk.gtt_preview(gtt(), "paper")["token"], "gtt buy 5 reliance")
    gtt_id = placed["order_id"]
    assert desk.check_paper_gtts() == [] and desk.gtt_status("paper", gtt_id)["status"] == "active"
    prices["p"] = 1349.0                                           # falls through the trigger
    fired = desk.check_paper_gtts()
    assert fired[0]["status"] == "triggered" and fired[0]["fill_price"] == 1349.0
    assert desk.paper.quantity("NSE:RELIANCE") == 5 and desk.check_paper_gtts() == []      # fires once
    assert desk.orders_today("paper") == 1                         # placing the GTT counts once; its fill does not

    exits = gtt(side="SELL", kind="oco", target=1420.0, stop=1280.0)
    assert any("must lie between" in e for e in desk.gtt_preview({**exits, "target": 1340.0, "stop": 1300.0}, "paper")["errors"])
    assert any("cannot sell" in e for e in desk.gtt_preview({**exits, "quantity": 9}, "paper")["errors"])
    oco = desk.auto_gtt(exits, "paper", "test")["order_id"]
    prices["p"] = 1425.0
    fired = desk.check_paper_gtts()
    assert fired[0]["leg"] == "target" and desk.paper.quantity("NSE:RELIANCE") == 0
    assert desk.gtt_status("paper", oco)["status"] == "triggered"

    pending = desk.auto_gtt(gtt(trigger=1300.0), "paper", "test")["order_id"]
    desk.cancel_gtt("paper", pending)
    prices["p"] = 1200.0
    assert desk.check_paper_gtts() == [] and desk.gtt_status("paper", pending)["status"] == "cancelled"
    clock.set("2026-11-16 20:00")
    desk.auto_gtt(gtt(trigger=1100.0), "paper", "test")
    prices["p"] = 1000.0
    assert desk.check_paper_gtts() == []                           # nothing triggers while the market is closed


def test_a_live_gtt_reaches_kite_with_the_right_legs(tmp_path):
    kite = KiteMCP()
    kite.connect(FAKE)
    try:
        kite.login()
        desk, _ = make_desk(tmp_path, Clock("2026-11-16 10:00"), kite=kite)
        assert any("switched off" in e for e in desk.gtt_preview(gtt(), "live")["errors"])
        desk.settings.update({"live_enabled": True})
        single = desk.auto_gtt(gtt(), "live", "test")["order_id"]
        oco = desk.auto_gtt(gtt(side="SELL", kind="oco", target=1600.0, stop=1400.0, tradingsymbol="INFY", quantity=20), "live", "test")["order_id"]
        listed = {str(row["id"]): row for row in kite.gtts()}
        assert listed[single]["type"] == "single" and listed[single]["condition"]["trigger_values"] == [1350.0]
        assert listed[single]["condition"]["last_price"] == 1400.0
        assert listed[oco]["type"] == "two-leg" and listed[oco]["condition"]["trigger_values"] == [1400.0, 1600.0]
        assert desk.gtt_status("live", single)["status"] == "active"
        desk.cancel_gtt("live", single)
        assert desk.gtt_status("live", single)["status"] == "deleted" and desk.gtt_status("live", "999") is None
        desk.settings.update({"kill_switch": True})
        with pytest.raises(OrderRejected, match="kill switch"):
            desk.auto_gtt(gtt(), "live", "test")
    finally:
        kite.disconnect()


# -- algos: the daily queue and intraday --------------------------------------------

DOWN = [100.0] * 30 + [90.0] * 8
UP = DOWN + [120.0] * 4


def algo_engine(tmp_path, feed, clock, **settings):
    desk, prices = make_desk(tmp_path, clock, price={"p": 100.0}, **settings)
    engine = AlgoEngine(desk, tmp_path / "algos.json", fetch=lambda s, interval="1d": feed["bars"], now=clock)
    return engine, prices


def test_a_daily_algo_queues_its_order_for_the_trade_window_and_protects_it_with_a_gtt(tmp_path):
    clock, feed = Clock("2026-11-16 16:00"), {"bars": flat(DOWN, "2026-11-16")}
    engine, prices = algo_engine(tmp_path, feed, clock)
    algo_id = engine.create({"name": "Rel", "symbol": "RELIANCE", "strategy": "sma_cross",
                             "strategy_params": {"fast": 3, "slow": 10}, "budget": 25_000, "mode": "paper",
                             "target_pct": 10, "stop_loss_pct": 5})["id"]
    engine.set_enabled(algo_id, True)
    engine.tick()
    clock.set("2026-11-20 16:00")
    feed["bars"] = flat(UP, "2026-11-20")
    prices["p"] = 120.0
    engine.tick()
    algo = engine.get(algo_id)
    assert algo["position"] == 0 and algo["queued"]["side"] == "BUY" and "queued for 09:30" in algo["status"]
    assert not engine.desk.journal_rows()                               # nothing sent after hours

    clock.set("2026-11-23 09:20")                                       # Monday, before the window
    engine.tick()
    assert engine.get(algo_id)["position"] == 0
    clock.set("2026-11-23 09:30")
    prices["p"] = 121.0
    engine.tick()
    algo = engine.get(algo_id)
    assert algo["position"] == 25_000 // market.round_to_tick(121.0 * 1.003, "BUY") and algo["queued"] is None
    assert algo["average_price"] == 121.0
    protect = engine.desk.paper.gtt(algo["protect"])
    assert protect["kind"] == "oco" and protect["target"] == pytest.approx(133.1) and protect["stop"] == pytest.approx(114.95)

    clock.set("2026-11-24 11:00")
    prices["p"] = 114.0                                                 # the stoploss leg triggers
    engine.tick()
    algo = engine.get(algo_id)
    assert algo["position"] == 0 and algo["protect"] is None and algo["blocked"]
    assert engine.desk.paper.quantity("NSE:RELIANCE") == 0
    assert any("stop" in e["message"] for e in algo["log"])
    # Still "long" by the strategy, but stopped out: no re-entry until it goes flat.
    clock.set("2026-11-24 16:00")
    feed["bars"] = flat(UP + [121.0, 120.0], "2026-11-24")
    engine.tick()
    assert engine.get(algo_id)["queued"] is None and "reset" in engine.get(algo_id)["status"]


def candles_for(day, closes, step=5):
    index = pd.DatetimeIndex([pd.Timestamp(day) + pd.Timedelta(hours=9, minutes=15 + step * k) for k in range(len(closes))])
    close = pd.Series(closes, index=index, dtype=float)
    return pd.DataFrame({"Open": close, "High": close, "Low": close, "Close": close, "Volume": 1000.0})


def test_an_intraday_algo_enters_in_the_window_takes_its_target_and_squares_off(tmp_path):
    history = pd.concat([candles_for(d, [100.0] * 75) for d in ("2026-11-12", "2026-11-13")])
    feed = {"bars": history}
    clock = Clock("2026-11-16 09:16")
    engine, prices = algo_engine(tmp_path, feed, clock)
    spec = {"name": "Intra", "symbol": "RELIANCE", "strategy": "ema_cross", "strategy_params": {"fast": 2, "slow": 5},
            "budget": 50_000, "mode": "paper", "interval": "5m", "target_pct": 2, "stop_loss_pct": 1}
    algo_id = engine.create(spec)["id"]
    assert engine.get(algo_id)["product"] == "MIS"
    engine.set_enabled(algo_id, True)

    def at(moment, closes, price):
        clock.set(f"2026-11-16 {moment}")
        feed["bars"] = pd.concat([history, candles_for("2026-11-16", closes)])
        prices["p"] = price
        engine._retry_at.clear()
        engine.tick()
        return engine.get(algo_id)

    algo = at("09:26", [100, 104], 104.0)                    # a BUY signal at 09:25, before the window
    assert algo["position"] == 0 and "outside the entry window" in algo["status"]
    algo = at("09:36", [100, 104, 105, 106], 106.0)          # still long by the strategy, window open
    assert algo["position"] == 50_000 // market.round_to_tick(106.0 * 1.003, "BUY") and algo["average_price"] == 106.0
    row = engine.desk.journal_rows()[0]
    assert row["product"] == "MIS" and row["source"] == "algo:Intra"

    algo = at("09:41", [100, 104, 105, 106, 106.5], 108.5)   # +2.4%: the target is hit on the live price
    assert algo["position"] == 0 and algo["blocked"] and any("target" in e["message"] for e in algo["log"])
    algo = at("09:46", [100, 104, 105, 106, 106.5, 108], 108.0)
    assert algo["position"] == 0                             # no re-entry while the signal has not reset

    algo = at("10:36", [100, 104, 105, 106, 106.5, 108] + [90.0] * 10, 90.0)
    assert algo["position"] == 0 and not algo["blocked"]     # the strategy went flat: the block lifts
    closes = [100, 104, 105, 106, 106.5, 108] + [90.0] * 20 + [95.0, 99.0, 104.0, 109.0]
    algo = at("11:46", closes, 109.0)                        # a fresh cross
    assert algo["position"] > 0 and not algo["blocked"]
    held = algo["position"]
    algo = at("15:00", closes + [110.0] * 39, 110.0)         # square-off time
    assert algo["position"] == 0 and any("square-off" in e["message"] for e in algo["log"])
    assert [r["side"] for r in engine.desk.journal_rows()][:2] == ["SELL", "BUY"]
    assert engine.desk.paper.quantity("NSE:RELIANCE") == 0 and held > 0
    algo = at("15:05", closes + [110.0] * 40, 112.0)
    assert algo["position"] == 0 and algo["status"] == "Running: done for the day"


def test_an_intraday_algo_stops_out_and_refuses_stale_candles(tmp_path):
    history = pd.concat([candles_for(d, [100.0] * 75) for d in ("2026-11-12", "2026-11-13")])
    feed, clock = {"bars": history}, Clock("2026-11-16 10:01")
    engine, prices = algo_engine(tmp_path, feed, clock)
    algo_id = engine.create({"name": "Intra", "symbol": "RELIANCE", "strategy": "ema_cross",
                             "strategy_params": {"fast": 2, "slow": 5}, "budget": 50_000, "mode": "paper",
                             "interval": "5m", "stop_loss_pct": 1})["id"]
    engine.set_enabled(algo_id, True)
    feed["bars"] = pd.concat([history, candles_for("2026-11-16", [100, 101, 102, 103, 104, 105, 106, 107, 108])])
    prices["p"] = 108.0
    engine.tick()
    assert engine.get(algo_id)["position"] > 0
    clock.set("2026-11-16 10:02")
    prices["p"] = 106.5                                       # -1.4% from 108
    engine._retry_at.clear()
    engine.tick()
    algo = engine.get(algo_id)
    assert algo["position"] == 0 and any("stoploss" in e["message"] for e in algo["log"])

    clock.set("2026-11-16 11:30")                             # the feed has not moved for 45 minutes
    engine._retry_at.clear()
    engine.tick()
    assert engine.get(algo_id)["status"] == "Blocked: candles are delayed"


def test_algo_settings_are_validated(tmp_path):
    engine, _ = algo_engine(tmp_path, {"bars": flat(DOWN, "2026-11-16")}, Clock("2026-11-16 10:00"))
    base = {"name": "A", "symbol": "TCS", "strategy": "orb", "budget": 20000, "mode": "paper"}
    for change, fragment in (({}, "intraday candle"), ({"interval": "2h"}, "Candle size"),
                             ({"interval": "5m", "target_pct": 80}, "Target"),
                             ({"interval": "5m", "stop_loss_pct": -1}, "Stoploss")):
        with pytest.raises(ValueError, match=fragment):
            engine.create({**base, **change})
    assert engine.create({**base, "interval": "15m"})["product"] == "MIS"


# -- scanner -------------------------------------------------------------------------

def breakout_bars(end="2026-11-16", hold=(105.5, 105.4, 105.6, 105.5), volume=4000.0):
    n = 36 + len(hold)
    index = pd.bdate_range(end=end, periods=n)
    close = pd.Series(100.0, index=index)
    bars = pd.DataFrame({"Open": close, "High": close + 1, "Low": close - 1, "Close": close, "Volume": 1000.0})
    bars.iloc[35] = [100, 106, 99.5, 105.5, volume]
    for offset, value in enumerate(hold, 36):
        bars.iloc[offset] = [value, value + 0.4, value - 0.4, value, 900.0]
    return bars


def test_the_breakout_setup_needs_volume_a_new_high_and_a_tight_hold():
    params = scanner_module.params_of(None)
    setup = find_setup(breakout_bars(), params)
    assert setup["breakout_date"] == "2026-11-10" and setup["age"] == 4 and setup["volume_multiple"] == 4.0
    assert setup["buy_trigger"] == 99.5 and setup["stop_loss"] == pytest.approx(94.5) and setup["target"] is None
    assert find_setup(breakout_bars(volume=2500.0), params) is None                 # under 3x average volume
    assert find_setup(breakout_bars(hold=(105.5, 108.0, 105.5)), params) is None    # ran more than 1% above the high
    assert find_setup(breakout_bars(hold=(105.5, 103.0)), params) is None           # fell more than 1% below
    assert find_setup(breakout_bars(hold=()), params)["age"] == 0                   # the breakout day itself
    old = breakout_bars(hold=tuple([105.5] * 25))
    assert find_setup(old, params) is None                                          # older than max_age
    assert find_setup(breakout_bars(), {**params, "tolerance_pct": 0.1}) is None
    with pytest.raises(ValueError, match="volume multiple"):
        scanner_module.params_of({"volume_multiple": 0})
    with pytest.raises(ValueError, match="not an NSE/BSE stock"):
        scanner_module.universe_of("NIFTY")


def test_the_scanner_places_each_setup_once_and_adds_the_stop_after_the_buy(tmp_path):
    clock = Clock("2026-11-16 16:00")
    desk, prices = make_desk(tmp_path, clock, price={"p": 105.5})

    def fetch(symbol, interval="1d"):
        if symbol == "TCS.NS":
            return breakout_bars()
        if symbol == "BAD.NS":
            return None
        return flat([100.0] * 60, "2026-11-16")

    scan = Scanner(desk, tmp_path / "scanner.json", fetch=fetch, now=clock)
    assert len(scan.view()["universe"]) > 90
    scan.configure(universe="TCS INFY BAD", params={**scanner_module.DEFAULTS, "target_pct": 8.0})
    result = scan.scan()
    assert [s["symbol"] for s in result["signals"]] == ["TCS.NS"] and result["signals"][0]["state"] == "new"
    assert result["scanned"] == 3 and [f["symbol"] for f in result["failed"]] == ["BAD.NS"]

    placed = scan.place("paper")["results"]
    assert placed[0]["outcome"] == "placed" and placed[0]["quantity"] == 50_000 // 99.5
    again = scan.place("paper")["results"]                                   # "day two": no duplicate
    assert again[0]["outcome"] == "already placed" and len(desk.paper.gtts()) == 1
    assert scan.scan()["signals"][0]["state"] == "buy_active"
    assert Scanner(desk, tmp_path / "scanner.json", fetch=fetch, now=clock).view()["trades"][0]["state"] == "buy_active"

    clock.set("2026-11-17 10:00")
    prices["p"] = 99.0                                                       # pulls back to the breakout low
    desk.check_paper_gtts()
    scan.tick()
    assert scan.view()["trades"][0]["state"] == "buy_active"                 # not followed until Run live is on
    scan.set_live(True)
    scan.tick()
    trade = scan.view()["trades"][0]
    assert trade["state"] == "holding" and trade["entry"] == 99.0
    exit_gtt = desk.paper.gtt(trade["exit_gtt"])
    assert exit_gtt["kind"] == "oco" and exit_gtt["stop"] == pytest.approx(94.5) and exit_gtt["quantity"] == trade["quantity"]
    scan.tick()
    assert len(desk.paper.gtts()) == 2                                       # the stop is placed once

    prices["p"] = 94.0
    desk.check_paper_gtts()
    scan.tick()
    trade = scan.view()["trades"][0]
    assert trade["state"] == "closed" and "stop" in trade["note"] and desk.paper.quantity("NSE:TCS") == 0
    with pytest.raises(ValueError, match="no such"):
        scan.cancel("nope")
    scan.forget(trade["key"])
    assert scan.view()["trades"] == []


def test_scanner_orders_respect_limits_and_can_be_cancelled(tmp_path):
    clock = Clock("2026-11-16 16:00")
    desk, _ = make_desk(tmp_path, clock, price={"p": 105.5}, kill_switch=True)
    scan = Scanner(desk, tmp_path / "scanner.json", fetch=lambda s, interval="1d": breakout_bars(), now=clock)
    with pytest.raises(ValueError, match="Run a scan first"):
        scan.place("paper")
    scan.configure(universe="TCS")
    scan.scan()
    assert scan.place("paper")["results"][0]["outcome"] == "refused"          # kill switch
    desk.settings.update({"kill_switch": False, "max_order_value": 50.0})
    assert scan.place("paper")["results"][0]["outcome"] == "skipped"          # limits buy no share
    desk.settings.update({"max_order_value": 50_000.0})
    key = scan.place("paper")["results"][0]["key"]
    with pytest.raises(ValueError, match="Cancel it first"):
        scan.forget(key)
    scan.cancel(key)
    assert scan.view()["trades"][0]["state"] == "cancelled" and desk.paper.gtts()[0]["status"] == "cancelled"


def test_an_intraday_algo_reenters_when_the_price_comes_back_after_a_points_stoploss(tmp_path):
    history = pd.concat([candles_for(d, [100.0] * 75) for d in ("2026-11-12", "2026-11-13")])
    feed, clock = {"bars": history}, Clock("2026-11-16 10:01")
    engine, prices = algo_engine(tmp_path, feed, clock)
    algo_id = engine.create({"name": "Again", "symbol": "RELIANCE", "strategy": "ema_cross",
                             "strategy_params": {"fast": 2, "slow": 5}, "budget": 50_000, "mode": "paper", "interval": "5m",
                             "exits": {"unit": "points", "stop_loss": 2, "reentry": 1}})["id"]
    engine.set_enabled(algo_id, True)
    feed["bars"] = pd.concat([history, candles_for("2026-11-16", [100, 101, 102, 103, 104, 105, 106, 107, 108])])
    prices["p"] = 108.0
    engine.tick()
    entry = engine.get(algo_id)["average_price"]
    for minute, price, held in (("10:02", entry - 2.5, False),    # stoploss 2 points under the entry: sold
                                ("10:03", entry - 1.0, False),    # not back at the entry yet
                                ("10:04", entry + 0.1, True)):    # back: bought again
        clock.set(f"2026-11-16 {minute}")
        prices["p"] = price
        engine._retry_at.clear()
        engine.tick()
        assert (engine.get(algo_id)["position"] > 0) == held, minute
    algo = engine.get(algo_id)
    assert algo["reentries_left"] == 0 and any("re-entry after stoploss" in e["message"] for e in algo["log"])
    clock.set("2026-11-16 10:05")
    prices["p"] = algo["average_price"] - 3                          # out again; no re-entry left
    engine._retry_at.clear()
    engine.tick()
    assert engine.get(algo_id)["position"] == 0 and engine.get(algo_id)["rearm"] is None
    assert engine.list()[0]["exits_text"] == "stoploss 2 pts, re-enter up to 1× when price comes back"


def test_a_daily_algo_moves_its_gtt_stoploss_to_cost(tmp_path):
    clock, feed = Clock("2026-11-16 16:00"), {"bars": flat(DOWN, "2026-11-16")}
    engine, prices = algo_engine(tmp_path, feed, clock)
    algo_id = engine.create({"name": "Cost", "symbol": "RELIANCE", "strategy": "sma_cross",
                             "strategy_params": {"fast": 3, "slow": 10}, "budget": 25_000, "mode": "paper",
                             "exits": {"unit": "pct", "stop_loss": 5, "cost_after": 5}})["id"]
    engine.set_enabled(algo_id, True)
    engine.tick()
    clock.set("2026-11-20 16:00")
    feed["bars"] = flat(UP, "2026-11-20")
    prices["p"] = 120.0
    engine.tick()
    clock.set("2026-11-23 09:30")
    prices["p"] = 121.0
    engine.tick()
    algo = engine.get(algo_id)
    assert engine.desk.paper.gtt(algo["protect"])["trigger"] == pytest.approx(114.95)
    clock.set("2026-11-23 16:00")
    feed["bars"] = flat(UP + [128.0], "2026-11-23")                  # a close 5.8% above the buy
    prices["p"] = 128.0
    engine.tick()
    algo = engine.get(algo_id)
    assert engine.desk.paper.gtt(algo["protect"])["trigger"] == pytest.approx(121.0)
    assert algo["stop_kind"] == "stoploss at cost"


def test_editing_a_stock_algo_keeps_its_position_safe(tmp_path):
    clock, feed = Clock("2026-11-16 16:00"), {"bars": flat(DOWN, "2026-11-16")}
    engine, prices = algo_engine(tmp_path, feed, clock)
    base = {"name": "Rel", "symbol": "RELIANCE", "strategy": "sma_cross", "strategy_params": {"fast": 3, "slow": 10},
            "budget": 25_000, "mode": "paper", "exits": {"unit": "pct", "stop_loss": 5, "target": 10}}
    algo_id = engine.create(base)["id"]
    engine.set_enabled(algo_id, True)
    engine.tick()
    # Flat: anything but the name can change, and a new stock or strategy waits for a fresh signal.
    edited = engine.update(algo_id, {**base, "name": "Other", "symbol": "TCS", "budget": 30_000})
    assert edited["name"] == "Rel" and edited["symbol"] == "TCS.NS" and edited["budget"] == 30_000
    assert edited["enabled"] and edited["last_bar"] is None and not edited["seen_flat"]
    engine.update(algo_id, base)
    engine.tick()
    clock.set("2026-11-20 16:00")
    feed["bars"], prices["p"] = flat(UP, "2026-11-20"), 120.0
    engine.tick()
    clock.set("2026-11-23 09:30")
    prices["p"] = 121.0
    engine.tick()
    algo = engine.get(algo_id)
    held, gtt = algo["position"], algo["protect"]
    assert held and gtt
    # Holding: the stock, strategy, candle and mode are tied to the position.
    for change in ({"symbol": "TCS"}, {"interval": "15m"}, {"strategy_params": {"fast": 4, "slow": 10}},
                   {"mode": "live"}):
        with pytest.raises(AlgoError, match="holds"):
            engine.update(algo_id, {**base, **change})
    # The budget and the exit rules can change; the paper GTT is replaced at the new levels.
    edited = engine.update(algo_id, {**base, "budget": 40_000, "exits": {"unit": "pct", "stop_loss": 8, "target": 20}})
    assert edited["position"] == held and edited["average_price"] == 121.0 and edited["budget"] == 40_000
    assert edited["protect"] and edited["protect"] != gtt
    protect = engine.desk.paper.gtt(edited["protect"])
    assert protect["target"] == pytest.approx(145.2) and protect["stop"] == pytest.approx(111.3)
    assert engine.desk.paper.gtt(gtt)["status"] != "active"
    assert any("Edited" in e["message"] for e in edited["log"])


def test_a_live_running_stock_algo_cannot_be_edited(tmp_path):
    clock, feed = Clock("2026-11-16 16:00"), {"bars": flat(DOWN, "2026-11-16")}
    engine, _ = algo_engine(tmp_path, feed, clock, live_enabled=True)
    base = {"name": "Live one", "symbol": "RELIANCE", "strategy": "sma_cross", "strategy_params": {"fast": 3, "slow": 10},
            "budget": 25_000, "mode": "live"}
    algo_id = engine.create(base)["id"]
    engine.set_enabled(algo_id, True, "ARM LIVE LIVE ONE")
    with pytest.raises(AlgoError, match="trading live"):
        engine.update(algo_id, {**base, "budget": 30_000})
    engine.set_enabled(algo_id, False)
    assert engine.update(algo_id, {**base, "budget": 30_000})["budget"] == 30_000
    paper_id = engine.create({**base, "name": "Paper one", "mode": "paper"})["id"]
    engine.set_enabled(paper_id, True)
    moved = engine.update(paper_id, {**base, "mode": "live"})
    assert moved["mode"] == "live" and not moved["enabled"]
