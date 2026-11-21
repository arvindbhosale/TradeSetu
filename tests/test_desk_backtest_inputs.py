"""Every backtest input changes the result when it should: stoploss, target, re-entry, and where the prices came from."""

import numpy as np
import pandas as pd
import pytest

from desk import data, futures as fut, options as opt, optstrat
from desk.backtest import BacktestParams, run_strategy
from desk.risk import ExitRules


def three_minute(days=30, seed=3, vol=0.004):
    stamps = []
    for d in pd.bdate_range("2026-09-01", periods=days):
        stamps += list(pd.date_range(d + pd.Timedelta(hours=9, minutes=15), d + pd.Timedelta(hours=15, minutes=27), freq="3min"))
    index = pd.DatetimeIndex(stamps)
    rng = np.random.default_rng(seed)
    close = 12000 * np.exp(np.cumsum(rng.normal(0, vol, len(index))))
    open_ = np.r_[close[0], close[:-1]]
    return pd.DataFrame({"Open": open_, "High": np.maximum(open_, close) * 1.001, "Low": np.minimum(open_, close) * 0.999,
                         "Close": close, "Volume": 1000.0}, index=index)


def run(bars, **exits):
    params = BacktestParams(capital=1_000_000, intraday=True, trade_start="09:20", trade_end="15:00", square_off="15:10",
                            exits=ExitRules(**{"unit": "pct", **exits}))
    return run_strategy(bars, "ema_cross", {"fast": 5, "slow": 20}, params)


def test_a_reachable_stoploss_and_target_change_every_figure():
    bars = three_minute()
    none, tight = run(bars), run(bars, stop_loss=0.5, target=1.0)
    exits = {t["exit"] for t in tight["trades"]}
    assert "stoploss" in exits and "target" in exits
    for key in ("total_return_pct", "final_value", "win_rate_pct", "total_charges"):
        assert none["metrics"][key] != tight["metrics"][key], key
    assert tight["metrics"]["exits"]["by_reason"]["stoploss"] > 0
    assert not tight["metrics"]["exits"]["stop_unused"] and not tight["metrics"]["exits"]["target_unused"]


def test_a_stoploss_no_trade_came_near_is_reported_not_silently_ignored():
    # Intraday trades on a share that moves well under 15% a day: a 15% stoploss and 45% target cannot fire.
    bars = three_minute(vol=0.001)
    none, wide = run(bars), run(bars, stop_loss=15, target=45)
    assert none["metrics"]["total_return_pct"] == wide["metrics"]["total_return_pct"]
    summary = wide["metrics"]["exits"]
    assert summary["stop_unused"] and summary["target_unused"]
    assert -15 < summary["deepest_fall_pct"] < 0 < summary["highest_rise_pct"] < 45
    assert all(t["worst_pct"] <= 0 <= t["best_pct"] for t in wide["trades"])


def test_reentry_after_a_stoploss_adds_trades_and_marks_them():
    bars = three_minute()
    plain = run(bars, stop_loss=0.4)
    again = run(bars, stop_loss=0.4, reentry=2, reentry_mode="at_once")
    marked = [t for t in again["trades"] if t["reentry"]]
    assert again["metrics"]["reentries"] == len(marked) > 0
    assert len(again["trades"]) > len(plain["trades"]) and not any(t["reentry"] for t in plain["trades"])
    assert again["metrics"]["total_return_pct"] != plain["metrics"]["total_return_pct"]
    back = run(bars, stop_loss=0.4, reentry=2)                       # when the price comes back to the entry
    assert back["metrics"]["reentries"] == sum(t["reentry"] for t in back["trades"]) > 0


def test_futures_stoploss_and_target_change_the_result():
    bars = three_minute()
    signal = pd.Series(1, index=bars.index)
    base = {"underlying": "NIFTY", "lot_size": 75, "capital": 5_000_000, "intraday": True, "trade_start": "09:20",
            "trade_end": "15:00", "square_off": "15:10", "continuous": False}
    none = fut.run_futures(bars, signal, **base)
    tight = fut.run_futures(bars, signal, **base, stop_pct=0.3, target_pct=0.6)
    assert none["metrics"]["total_return_pct"] != tight["metrics"]["total_return_pct"]
    assert {"stoploss", "target"} & {t["exit"] for t in tight["trades"]}


def test_option_buying_stoploss_and_target_change_the_result():
    bars = three_minute(days=10).groupby(lambda t: t.normalize()).agg(
        {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"})
    bars = pd.concat([bars] * 4).reset_index(drop=True)
    bars.index = pd.bdate_range("2026-01-05", periods=len(bars))
    vix = pd.Series(14.0, index=pd.date_range("2025-12-01", "2026-06-01"))
    positions = pd.Series(1, index=bars.index)
    base = {"capital": 1_000_000, "lots": 1, "lot_size": 75}
    none = opt.run_options("NIFTY", bars, positions, vix, **base)
    tight = opt.run_options("NIFTY", bars, positions, vix, **base, stop_pct=10, target_pct=10)
    assert none["metrics"]["total_return_pct"] != tight["metrics"]["total_return_pct"]


def test_structure_stoploss_changes_the_result():
    bars = three_minute(days=6, vol=0.0015) * 2
    vix = pd.Series(14.0, index=pd.date_range("2026-08-01", "2026-11-01"))
    base = {"structure": "short_straddle", "mode": "intraday", "lot_size": 75, "expiry_kind": "weekly"}
    none = optstrat.run_structure("NIFTY", bars, vix, **base)
    tight = optstrat.run_structure("NIFTY", bars, vix, **base, leg_exits=ExitRules(unit="pct", stop_loss=10))
    assert none["metrics"]["total_return_pct"] != tight["metrics"]["total_return_pct"]


# -- prices: Kite's history is fetched in chunks, back to 2015, with splits adjusted -----------------------

class RecordingKite:
    def __init__(self):
        self.calls = []

    def candles(self, instrument, interval, start, end, continuous=False):
        self.calls.append((instrument, interval, start, end))
        days = pd.bdate_range(start.date(), end.date())
        return [{"date": f"{d:%Y-%m-%d}T09:15:00+0530", "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1}
                for d in days]


@pytest.fixture
def kite(monkeypatch):
    monkeypatch.setattr(data.time, "sleep", lambda s: None)
    monkeypatch.setattr(data, "kite_instrument_for", lambda symbol: "NSE:MARUTI")
    data._range_cache.clear()
    return RecordingKite()


def test_three_minute_candles_reach_back_years_in_kites_100_day_requests(kite):
    frame = data.kite_range(kite, "MARUTI", "3m", "2021-01-04", "2026-11-21")
    assert frame.index[0] == pd.Timestamp("2021-01-04 09:15") and frame.index[-1] >= pd.Timestamp("2026-11-20")
    spans = [(end - start).days for _, _, start, end in kite.calls]
    assert max(spans) < 100 and len(kite.calls) == 22
    assert frame.attrs["from_clipped"] is None
    data.kite_range(kite, "MARUTI", "3m", "2021-01-04", "2026-11-21")           # a re-run (other exits) fetches nothing
    assert len(kite.calls) == 22


def test_intraday_before_2015_starts_where_kite_history_does(kite):
    frame = data.kite_range(kite, "MARUTI", "15m", "2012-01-02", "2015-06-30")
    assert frame.index[0] >= data.KITE_INTRADAY_FROM and frame.attrs["from_clipped"] == pd.Timestamp("2012-01-02")
    day = data.kite_range(kite, "MARUTI", "1d", "2012-01-02", "2015-06-30")     # day candles go back further
    assert day.index[0] == pd.Timestamp("2012-01-02") and day.attrs["from_clipped"] is None


def test_an_unadjusted_bonus_is_folded_back_into_earlier_prices():
    index = pd.bdate_range("2026-01-01", periods=6)
    close = [1000.0, 1010, 1020, 512, 515, 520]                     # a 1:1 bonus: the price halves overnight
    bars = pd.DataFrame({"Open": close, "High": close, "Low": close, "Close": close, "Volume": 10.0}, index=index)
    fixed, found = data.adjust_splits(bars)
    assert found == [("06 Jan 2026", 2.0)]
    assert fixed["Close"].iloc[2] == 510 and fixed["Volume"].iloc[0] == 20
    real_fall = bars.copy()
    real_fall.loc[index[3]:, ["Open", "High", "Low", "Close"]] = 850.0  # a 17% fall is a fall
    assert data.adjust_splits(real_fall)[1] == []


def test_a_small_ratio_gap_is_adjusted_only_when_the_adjusted_feed_shows_no_fall():
    index = pd.bdate_range("2026-01-01", periods=6)
    close = [1000.0, 1000, 1000, 800, 805, 810]                     # 1.25: a 1:4 bonus, or a 20% lower-circuit open
    bars = pd.DataFrame({"Open": close, "High": close, "Low": close, "Close": close, "Volume": 1.0}, index=index)
    assert data.adjust_splits(bars)[1] == []                         # unconfirmed: left as a real fall
    flat = bars.copy()
    flat.loc[index[:3], ["Open", "High", "Low", "Close"]] = 800.0   # the adjusted feed: no gap, so a bonus
    assert data.adjust_splits(bars, reference=lambda: flat)[1] == [("06 Jan 2026", 1.25)]
    assert data.adjust_splits(bars, reference=lambda: bars)[1] == []                 # same fall there: real
    halved = bars.copy()
    halved.loc[index[3]:, ["Open", "High", "Low", "Close"]] = 500.0
    assert data.adjust_splits(halved, reference=lambda: halved)[1] == []             # even a halving, if real
