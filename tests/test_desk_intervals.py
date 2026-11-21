"""2, 3, 4 and 30-minute candles, and traditional pivots (daily, weekly, monthly)."""

import numpy as np
import pandas as pd
import pytest

from desk import custom, data, indicators as ind
from desk.kite import KiteMCP


def minutes(day, start="09:15", end="15:29", seed=1):
    index = pd.date_range(f"{day} {start}", f"{day} {end}", freq="1min")
    rng = np.random.default_rng(seed)
    close = 100 + np.cumsum(rng.normal(0, 0.1, len(index)))
    open_ = np.r_[close[0], close[:-1]]
    return pd.DataFrame({"Open": open_, "High": np.maximum(open_, close) + 0.05, "Low": np.minimum(open_, close) - 0.05,
                         "Close": close, "Volume": 10.0}, index=index)


def test_the_new_sizes_are_offered_everywhere_in_order():
    assert list(data.INTERVALS) == ["1m", "2m", "3m", "4m", "5m", "15m", "30m", "1h", "1d"]
    for key in data.INTRADAY:
        assert key in data.KITE_LOOKBACK and key in data.KITE_CHUNK
    # Kite's own sizes are used where it has them; 2 and 4 minutes are built from its minute candles.
    assert {k: v["kite"] for k, v in data.INTERVALS.items() if not v.get("kite_resample")}["3m"] == "3minute"
    assert data.INTERVALS["30m"]["kite"] == "30minute"
    assert [k for k, v in data.INTERVALS.items() if v.get("kite_resample")] == ["2m", "4m"]


@pytest.mark.parametrize(("size", "count", "last", "last_minutes"),
                         [(2, 188, "15:29", 1), (3, 125, "15:27", 3), (4, 94, "15:27", 3), (30, 13, "15:15", 15)])
def test_resampling_lines_up_with_the_session_open_and_keeps_the_short_last_candle(size, count, last, last_minutes):
    bars = minutes("2026-11-16")
    out = data.resample(bars, size)
    assert len(out) == count and out.index[0] == pd.Timestamp("2026-11-16 09:15")
    assert out.index[1] - out.index[0] == pd.Timedelta(minutes=size)
    assert out.index[-1] == pd.Timestamp(f"2026-11-16 {last}")
    tail = bars[bars.index >= out.index[-1]]
    assert len(tail) == last_minutes and out["Volume"].iloc[-1] == 10.0 * last_minutes
    first = bars.iloc[:size]
    row = out.iloc[0]
    assert (row["Open"], row["High"], row["Low"], row["Close"]) == (
        first["Open"].iloc[0], first["High"].max(), first["Low"].min(), first["Close"].iloc[-1])
    assert out["Volume"].sum() == bars["Volume"].sum()


def test_resampling_follows_an_earlier_open_and_a_forming_candle():
    mcx = data.resample(minutes("2026-11-16", "09:00", "09:13"), 4)        # MCX opens at 09:00
    assert [t.strftime("%H:%M") for t in mcx.index] == ["09:00", "09:04", "09:08", "09:12"]
    # Live: three minutes of a 4-minute candle so far. The partial candle is there with what it has; the
    # algo drops it by its nominal end, as for every size.
    live = data.resample(minutes("2026-11-17", "09:15", "09:29"), 4)
    assert live.index[-1] == pd.Timestamp("2026-11-17 09:27") and live["Volume"].iloc[-1] == 30.0
    now = pd.Timestamp("2026-11-17 09:30")
    finished = live[live.index + pd.Timedelta(minutes=4) <= now]
    assert finished.index[-1] == pd.Timestamp("2026-11-17 09:23")
    # Two days never merge, and a day fetched from mid-session still lines up at 09:15.
    two = data.resample(pd.concat([minutes("2026-11-16", "10:02"), minutes("2026-11-17")]), 30)
    assert two.index[0] == pd.Timestamp("2026-11-16 09:45") and pd.Timestamp("2026-11-17 09:15") in two.index


def kite_with(rows):
    kite = KiteMCP()
    kite.tools = {"get_historical_data": {}}
    kite._tokens = {"NFO:NIFTY26NOVFUT": 7}
    seen = []

    def call(tool, args=None, timeout=45.0):
        seen.append(args)
        return rows
    kite.call = call
    return kite, seen


def kite_rows(bars, oi=False):
    return [{"date": t.strftime("%Y-%m-%dT%H:%M:%S+0530"), "open": r.Open, "high": r.High, "low": r.Low,
             "close": r.Close, "volume": r.Volume, **({"oi": 1000.0 + i} if oi else {})}
            for i, (t, r) in enumerate(zip(bars.index, bars.itertuples(), strict=True))]


def test_kite_builds_2_and_4_minute_candles_from_its_minute_candles():
    bars = minutes("2026-11-16")
    kite, seen = kite_with(kite_rows(bars, oi=True))
    rows = kite.candles("NFO:NIFTY26NOVFUT", "4m", pd.Timestamp("2026-11-16 11:07").to_pydatetime(),
                        pd.Timestamp("2026-11-16 15:30").to_pydatetime(), oi=True)
    assert seen[0]["interval"] == "minute" and seen[0]["from_date"] == "2026-11-16 00:00:00" and seen[0]["oi"]
    frame = data.kite_frame(rows)
    expected = data.resample(bars, 4)
    assert len(frame) == len(expected) and frame.index[1] == pd.Timestamp("2026-11-16 09:19")
    assert np.allclose(frame["High"], expected["High"]) and np.allclose(frame["Close"], expected["Close"])
    assert rows[0]["oi"] == 1003.0                                      # open interest at the candle's close
    kite, seen = kite_with(kite_rows(data.resample(bars, 3)))
    rows = kite.candles("NFO:NIFTY26NOVFUT", "3m", pd.Timestamp("2026-11-16 11:07").to_pydatetime(),
                        pd.Timestamp("2026-11-16 15:30").to_pydatetime())
    assert seen[0]["interval"] == "3minute" and seen[0]["from_date"] == "2026-11-16 11:07:00" and len(rows) == 125
    kite, seen = kite_with([])
    assert kite.candles("NFO:NIFTY26NOVFUT", "2m", pd.Timestamp("2026-11-16").to_pydatetime(),
                        pd.Timestamp("2026-11-16 15:30").to_pydatetime()) == []


def test_yahoo_3_minute_candles_are_built_from_1_minute(tmp_path, monkeypatch):
    asked = []
    bars = minutes("2026-11-16")

    def yahoo(symbol, interval="1d"):
        asked.append(data.INTERVALS[interval]["yahoo"])
        return bars
    monkeypatch.setattr(data, "_fetch_yahoo", yahoo)
    out = data.candles("RELIANCE", "3m", cache_dir=tmp_path)
    assert asked == ["1m"] and len(out) == 125 and out.index[1] == pd.Timestamp("2026-11-16 09:18")


def test_traditional_pivots_daily_weekly_monthly_and_never_from_a_partial_period():
    days = pd.concat([minutes(d, seed=i) for i, d in enumerate(pd.bdate_range("2026-11-02", "2026-11-17").strftime("%Y-%m-%d"))])
    bars = data.resample(days, 5)
    daily = ind.period_pivots(bars, "D")
    monday = bars.loc["2026-11-16"]
    expected = ind.pivot_levels(monday["High"].max(), monday["Low"].min(), monday["Close"].iloc[-1])
    assert daily.loc["2026-11-17"].drop_duplicates().shape[0] == 1
    for name, value in expected.items():
        assert daily.loc["2026-11-17 10:00", name] == pytest.approx(value)
    weekly = ind.period_pivots(bars, "W")
    week = bars.loc["2026-11-09":"2026-11-13"]
    assert weekly.loc["2026-11-17 10:00", "S3"] == pytest.approx(
        ind.pivot_levels(week["High"].max(), week["Low"].min(), week["Close"].iloc[-1])["S3"])
    assert weekly.loc["2026-11-09":"2026-11-13"].notna().all().all()        # the data starts on a Monday
    # A week the data joins on Wednesday cannot set the next week's levels; a day joined mid-session neither.
    late = bars.loc["2026-11-04":]
    assert ind.period_pivots(late, "W").loc["2026-11-09":"2026-11-13"].isna().all().all()
    midday = bars.loc["2026-11-16 11:00":]
    assert ind.period_pivots(midday, "D").loc["2026-11-17"].isna().all().all()
    assert ind.period_pivots(bars.loc["2026-11-16":], "D").loc["2026-11-17"].notna().all().all()
    assert ind.period_pivots(bars, "M").loc["2026-11"].isna().all().all()     # October is not in the data


def test_pivot_rules_in_the_strategy_builder_and_saved_monthly_keys_still_load():
    for freq, suffix in (("daily", "_d"), ("weekly", "_w"), ("monthly", "")):
        for level in ("R3", "R2", "R1", "P", "S1", "S2", "S3"):
            key = ("pivot" if level == "P" else f"pivot_{level.lower()}") + suffix
            assert ind.OPERANDS[key].label == f"Pivot {level} ({freq})"
    assert ind.OPERANDS["pivot_r1"].label == "Pivot R1 (monthly)"            # saved before: same meaning
    spec = custom.validate({"name": "Pivot bounce", "entry": {"match": "all", "rules": [
        {"left": {"indicator": "close"}, "op": "crosses_above", "right": {"indicator": "pivot_d"}}]},
        "exit": {"match": "any", "rules": [
            {"left": {"indicator": "high"}, "op": ">=", "right": {"indicator": "pivot_r1_d"}},
            {"left": {"indicator": "close"}, "op": "<", "right": {"indicator": "pivot_s1_w"}}]}})
    strategy = custom.as_strategy("x", spec)
    assert strategy.history_days == 14 and "Pivot P (daily)" in strategy.description
    days = pd.concat([minutes(d, seed=i) for i, d in enumerate(pd.bdate_range("2026-11-02", "2026-11-17").strftime("%Y-%m-%d"))])
    held = strategy.positions(data.resample(days, 3), {})
    assert set(held.unique()) <= {0, 1} and held.loc[:"2026-11-02"].eq(0).all()     # no levels on day one
