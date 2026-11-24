"""Intraday trades do not carry a buy signal overnight: latched rules must say buy again on today's candles."""

import numpy as np
import pandas as pd

from desk import custom
from desk.backtest import BacktestParams, run_strategy


def _bars(day_prices: dict[str, float]) -> pd.DataFrame:
    frames = []
    for day, base in day_prices.items():
        idx = pd.date_range(f"{day} 09:15", f"{day} 15:25", freq="5min")
        close = base + np.arange(len(idx)) * 0.01
        frames.append(pd.DataFrame({"Open": close, "High": close + 0.05, "Low": close - 0.05, "Close": close},
                                   index=idx))
    return pd.concat(frames)


def _rules(buy_above: float) -> dict:
    return {"entry": {"match": "all", "rules": [{"left": {"indicator": "close"}, "op": ">",
                                                 "right": {"value": buy_above}}]},
            "exit": {"match": "all", "rules": [{"left": {"indicator": "close"}, "op": "<", "right": {"value": 1}}]}}


def _run(bars, strategy, positional=False):
    params = BacktestParams(capital=1_000_000, intraday=True, positional=positional, trade_start="09:20",
                            trade_end="14:50", square_off="15:15", slippage_bps=0)
    return run_strategy(bars, strategy, None, params)["trades"]


def test_signal_from_yesterday_is_not_bought_today():
    # Day 1 trades above 150 (buy rule true); days 2 and 3 trade at 100 (rule false). The exit rule never fires,
    # so the position latch stays long, but no intraday buy may be made on days 2 and 3.
    bars = _bars({"2026-01-05": 200, "2026-01-06": 100, "2026-01-07": 100})
    trades = _run(bars, custom.inline(_rules(150)))
    assert [t["entry_date"][:10] for t in trades] == ["2026-01-05"]
    assert trades[0]["exit"] == "square-off"


def test_rules_true_again_today_buy_again():
    bars = _bars({"2026-01-05": 200, "2026-01-06": 190})
    trades = _run(bars, custom.inline(_rules(150)))
    assert [t["entry_date"] for t in trades] == ["2026-01-05 09:20", "2026-01-06 09:20"]


def test_positional_keeps_holding():
    bars = _bars({"2026-01-05": 200, "2026-01-06": 100})
    trades = _run(bars, custom.inline(_rules(150)), positional=True)
    assert len(trades) == 1 and trades[0]["open"]


def test_intraday_algo_waits_for_todays_buy_rules(tmp_path, monkeypatch):
    import pytest
    pytest.importorskip("mcp")
    monkeypatch.setenv("BHARAT_DESK_HOME", str(tmp_path / "desk"))
    import tradingagents.dataflows.config as config_module
    monkeypatch.setattr(config_module, "_config", {**config_module._config, "default_exchange": "NSE"})
    from desk.algo import AlgoEngine
    from desk.custom import CustomStrategies
    from tests.test_desk_trading import Clock, candles_for, make_desk

    library = CustomStrategies(tmp_path / "custom.json")
    key = library.save({"name": "Above 150", **_rules(150)})["key"]
    history = candles_for("2026-11-13", [200.0] * 75)            # Friday: the buy rule held, the latch is long
    feed, clock = {"bars": history}, Clock("2026-11-16 09:36")
    desk, prices = make_desk(tmp_path, clock, price={"p": 100.0})
    engine = AlgoEngine(desk, tmp_path / "algos.json", fetch=lambda s, interval="1d": feed["bars"], now=clock,
                        library=library)
    algo_id = engine.create({"name": "Intra", "symbol": "RELIANCE", "strategy": key, "budget": 50_000,
                             "mode": "paper", "interval": "5m"})["id"]
    engine.set_enabled(algo_id, True)

    def at(moment, closes, price):
        clock.set(f"2026-11-16 {moment}")
        feed["bars"] = pd.concat([history, candles_for("2026-11-16", closes)])
        prices["p"] = price
        engine._retry_at.clear()
        engine.tick()
        return engine.get(algo_id)

    algo = at("09:36", [100.0] * 4, 100.0)                       # Monday at 100: Friday's signal is not bought
    assert algo["position"] == 0 and "today's buy signal" in algo["status"]
    algo = at("09:41", [100.0] * 4 + [160.0], 160.0)             # the rule holds again today: buy
    assert algo["position"] > 0
