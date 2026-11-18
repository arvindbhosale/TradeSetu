"""Technical indicators on OHLCV bars. Every value on bar ``t`` uses bars up to ``t`` only.

Written from the indicators' published definitions. ``OPERANDS`` lists the
ones a custom strategy rule or a chart can name.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(period).mean()


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False, min_periods=period).mean()


def wilder(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain, loss = wilder(delta.clip(lower=0), period), wilder(-delta.clip(upper=0), period)
    out = 100 - 100 / (1 + gain / loss.replace(0, np.nan))
    return out.where(loss != 0, 100.0).where(gain.notna())


def true_range(bars: pd.DataFrame) -> pd.Series:
    prev = bars["Close"].shift(1)
    return pd.concat([bars["High"] - bars["Low"], (bars["High"] - prev).abs(),
                      (bars["Low"] - prev).abs()], axis=1).max(axis=1)


def atr(bars: pd.DataFrame, period: int = 14) -> pd.Series:
    return wilder(true_range(bars), period)


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    line = ema(close, fast) - ema(close, slow)
    sig = line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    return line, sig, line - sig


def bollinger(close: pd.Series, period: int = 20, width: float = 2.0):
    mid, sd = sma(close, period), close.rolling(period).std(ddof=0)
    return mid + width * sd, mid, mid - width * sd


def donchian(bars: pd.DataFrame, period: int = 20):
    """Highest high and lowest low of the ``period`` bars before this one."""
    return bars["High"].rolling(period).max().shift(1), bars["Low"].rolling(period).min().shift(1)


def supertrend(bars: pd.DataFrame, period: int = 10, multiplier: float = 3.0):
    """The trailing band and the trend (1 long, 0 short)."""
    band_atr = atr(bars, period)
    mid = (bars["High"] + bars["Low"]) / 2
    upper_basic, lower_basic = (mid + multiplier * band_atr).to_numpy(), (mid - multiplier * band_atr).to_numpy()
    close, n = bars["Close"].to_numpy(), len(bars)
    line, trend = np.full(n, np.nan), np.zeros(n, dtype=int)
    upper = lower = None
    long = False
    for i in range(n):
        if np.isnan(upper_basic[i]):
            continue
        if upper is None:
            upper, lower, long = upper_basic[i], lower_basic[i], close[i] > upper_basic[i]
        else:
            upper = upper_basic[i] if upper_basic[i] < upper or close[i - 1] > upper else upper
            lower = lower_basic[i] if lower_basic[i] > lower or close[i - 1] < lower else lower
            long = (close[i] >= lower) if long else (close[i] > upper)
        line[i], trend[i] = (lower if long else upper), int(long)
    return pd.Series(line, index=bars.index), pd.Series(trend, index=bars.index)


def adx(bars: pd.DataFrame, period: int = 14):
    """ADX with +DI and -DI (Wilder)."""
    up, down = bars["High"].diff(), -bars["Low"].diff()
    plus_dm = up.where((up > down) & (up > 0), 0.0)
    minus_dm = down.where((down > up) & (down > 0), 0.0)
    tr = wilder(true_range(bars), period)
    plus_di = 100 * wilder(plus_dm, period) / tr
    minus_di = 100 * wilder(minus_dm, period) / tr
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return wilder(dx, period), plus_di, minus_di


def stochastic(bars: pd.DataFrame, period: int = 14, smooth: int = 3):
    low, high = bars["Low"].rolling(period).min(), bars["High"].rolling(period).max()
    k = 100 * (bars["Close"] - low) / (high - low).replace(0, np.nan)
    return k, k.rolling(smooth).mean()


def cci(bars: pd.DataFrame, period: int = 20) -> pd.Series:
    typical = (bars["High"] + bars["Low"] + bars["Close"]) / 3
    mean = typical.rolling(period).mean()
    deviation = typical.rolling(period).apply(lambda w: np.abs(w - w.mean()).mean(), raw=True)
    return (typical - mean) / (0.015 * deviation.replace(0, np.nan))


def awesome_oscillator(bars: pd.DataFrame, fast: int = 5, slow: int = 34) -> pd.Series:
    median = (bars["High"] + bars["Low"]) / 2
    return sma(median, fast) - sma(median, slow)


def parabolic_sar(bars: pd.DataFrame, step: float = 0.02, maximum: float = 0.2):
    """Wilder's parabolic SAR and the trend (1 when price is above the SAR)."""
    high, low, n = bars["High"].to_numpy(float), bars["Low"].to_numpy(float), len(bars)
    sar, trend = np.full(n, np.nan), np.zeros(n, dtype=int)
    if n < 2:
        return pd.Series(sar, index=bars.index), pd.Series(trend, index=bars.index)
    long = high[1] >= high[0]
    extreme = high[1] if long else low[1]
    value = low[0] if long else high[0]
    factor = step
    sar[1], trend[1] = value, int(long)
    for i in range(2, n):
        value = value + factor * (extreme - value)
        if long:
            value = min(value, low[i - 1], low[i - 2])
            if low[i] < value:                       # stopped out: flip short
                long, value, extreme, factor = False, extreme, low[i], step
            elif high[i] > extreme:
                extreme, factor = high[i], min(factor + step, maximum)
        else:
            value = max(value, high[i - 1], high[i - 2])
            if high[i] > value:
                long, value, extreme, factor = True, extreme, high[i], step
            elif low[i] < extreme:
                extreme, factor = low[i], min(factor + step, maximum)
        sar[i], trend[i] = value, int(long)
    return pd.Series(sar, index=bars.index), pd.Series(trend, index=bars.index)


def heikin_ashi(bars: pd.DataFrame) -> pd.DataFrame:
    close = (bars["Open"] + bars["High"] + bars["Low"] + bars["Close"]) / 4
    open_ = np.empty(len(bars))
    if len(bars):
        open_[0] = (bars["Open"].iloc[0] + bars["Close"].iloc[0]) / 2
        values = close.to_numpy()
        for i in range(1, len(bars)):
            open_[i] = (open_[i - 1] + values[i - 1]) / 2
    open_ = pd.Series(open_, index=bars.index)
    return pd.DataFrame({"Open": open_, "High": pd.concat([bars["High"], open_, close], axis=1).max(axis=1),
                         "Low": pd.concat([bars["Low"], open_, close], axis=1).min(axis=1), "Close": close})


def vwap(bars: pd.DataFrame) -> pd.Series:
    """Volume-weighted average price, restarting each trading day."""
    typical = (bars["High"] + bars["Low"] + bars["Close"]) / 3
    day = bars.index.normalize()
    volume = bars["Volume"].astype(float)
    return (typical * volume).groupby(day).cumsum() / volume.groupby(day).cumsum().replace(0, np.nan)


PIVOT_LEVELS = ("R3", "R2", "R1", "P", "S1", "S2", "S3")
PIVOT_PERIODS = {"D": "daily", "W": "weekly", "M": "monthly"}


def pivot_levels(high, low, close) -> dict:
    """Traditional (classic floor-trader) pivots from one period's high, low and close.

    P = (H + L + C) / 3, R1 = 2P - L, S1 = 2P - H, R2 = P + (H - L), S2 = P - (H - L),
    R3 = H + 2(P - L), S3 = L - 2(H - P). Kite does not publish pivots; every chart and rule computes them here.
    """
    pivot = (high + low + close) / 3
    spread = high - low
    return {"R3": high + 2 * (pivot - low), "R2": pivot + spread, "R1": 2 * pivot - low, "P": pivot,
            "S1": 2 * pivot - high, "S2": pivot - spread, "S3": low - 2 * (high - pivot)}


def _first_period_complete(bars: pd.DataFrame, period: pd.PeriodIndex) -> bool:
    """Whether the data covers the whole of its first period, so the levels it sets for the next one are real.

    The first day of intraday data counts when it starts no later than the other days do (a fetch that
    begins mid-session does not). A week or month counts when no weekday of it comes before the first bar;
    an exchange holiday there reads as missing data, which costs a period of levels rather than giving
    wrong ones.
    """
    index = bars.index
    first = index[0]
    intraday = bool((index != index.normalize()).any())
    if intraday:
        day = index.normalize()
        opens = pd.Series(index - day, index=index).groupby(day).min()
        if len(opens) > 1 and opens.iloc[0] > opens.iloc[1:].min():
            return False
    if period.freqstr.startswith("D"):
        return True
    start = period[0].start_time.date()
    return int(np.busday_count(start, first.date())) == 0


def period_pivots(bars: pd.DataFrame, freq: str = "M") -> pd.DataFrame:
    """For each bar, the traditional pivots of its period (day ``D``, week ``W`` or month ``M``), set by the
    period before. Bars whose previous period the data does not wholly cover get no levels (NaN)."""
    period = bars.index.to_period(freq)
    ranges = bars.groupby(period).agg(High=("High", "max"), Low=("Low", "min"), Close=("Close", "last"))
    previous = ranges.shift(1)
    if len(ranges) > 1 and not _first_period_complete(bars, period):
        previous.iloc[1] = np.nan
    out = pd.DataFrame(pivot_levels(previous["High"], previous["Low"], previous["Close"])).reindex(period)
    out.index = bars.index
    return out


def time_of_day(bars: pd.DataFrame) -> pd.Series:
    """The candle's start time as an HHMM number: 09:20 is 920, 15:05 is 1505."""
    index = bars.index
    return pd.Series(index.hour * 100 + index.minute, index=index, dtype=float)


def day_of_week(bars: pd.DataFrame) -> pd.Series:
    """1 for Monday to 5 for Friday (6 and 7 for a weekend session)."""
    return pd.Series(bars.index.day_of_week + 1, index=bars.index, dtype=float)


WEEKDAYS = {1: "Mon", 2: "Tue", 3: "Wed", 4: "Thu", 5: "Fri", 6: "Sat", 7: "Sun"}


# -- operands a rule or chart can name -----------------------------------------

@dataclass(frozen=True)
class Operand:
    key: str
    label: str
    params: tuple        # of (name, label, default, minimum, maximum, integer)
    fn: object
    overlay: bool = True     # drawn on the price chart (same scale as price)
    history_days: int = 0    # calendar days of intraday history it needs before it has a value
    intraday: bool = False   # only means something on intraday candles (time of day, VWAP)
    kind: str = "number"     # what the right side of a rule takes: a number, a time (HH:MM) or weekdays


def _p(name, label, default, lo, hi, integer=True):
    return (name, label, default, lo, hi, integer)


_PERIOD = _p("period", "Period", 14, 2, 400)
# A previous week or month must be wholly in the data: up to 13 and 62 calendar days back.
_PIVOT_HISTORY = {"D": 5, "W": 14, "M": 62}


def _pivot_operands() -> list:
    """Traditional pivot levels, daily, weekly and monthly. Monthly keys carry no suffix (the first ones
    the builder had: ``pivot``, ``pivot_r1``, ``pivot_s1``), so strategies saved before keep working."""
    out = []
    for freq, word in PIVOT_PERIODS.items():
        suffix = "" if freq == "M" else "_" + freq.lower()
        for level in PIVOT_LEVELS:
            key = ("pivot" if level == "P" else f"pivot_{level.lower()}") + suffix
            out.append(Operand(key, f"Pivot {level} ({word})", (),
                               lambda b, freq=freq, level=level: period_pivots(b, freq)[level],
                               history_days=_PIVOT_HISTORY[freq]))
    return out

OPERANDS: dict[str, Operand] = {o.key: o for o in (
    Operand("close", "Close price", (), lambda b: b["Close"]),
    Operand("open", "Open price", (), lambda b: b["Open"]),
    Operand("high", "High price", (), lambda b: b["High"]),
    Operand("low", "Low price", (), lambda b: b["Low"]),
    Operand("prev_close", "Previous close", (), lambda b: b["Close"].shift(1)),
    Operand("sma", "SMA", (_p("period", "Period", 20, 2, 400),), lambda b, period: sma(b["Close"], period)),
    Operand("ema", "EMA", (_p("period", "Period", 20, 2, 400),), lambda b, period: ema(b["Close"], period)),
    Operand("vwap", "VWAP (day)", (), vwap, intraday=True),
    Operand("bb_upper", "Bollinger upper", (_p("period", "Period", 20, 5, 200), _p("width", "Width σ", 2.0, 0.5, 5, False)),
            lambda b, period, width: bollinger(b["Close"], period, width)[0]),
    Operand("bb_mid", "Bollinger middle", (_p("period", "Period", 20, 5, 200),),
            lambda b, period: bollinger(b["Close"], period)[1]),
    Operand("bb_lower", "Bollinger lower", (_p("period", "Period", 20, 5, 200), _p("width", "Width σ", 2.0, 0.5, 5, False)),
            lambda b, period, width: bollinger(b["Close"], period, width)[2]),
    Operand("supertrend", "Supertrend line", (_p("period", "ATR period", 10, 2, 100), _p("multiplier", "Multiplier", 3.0, 0.5, 10, False)),
            lambda b, period, multiplier: supertrend(b, period, multiplier)[0]),
    Operand("psar", "Parabolic SAR", (), lambda b: parabolic_sar(b)[0]),
    Operand("donchian_high", "Highest high of last N", (_p("period", "Bars", 20, 2, 300),), lambda b, period: donchian(b, period)[0]),
    Operand("donchian_low", "Lowest low of last N", (_p("period", "Bars", 20, 2, 300),), lambda b, period: donchian(b, period)[1]),
    *_pivot_operands(),
    Operand("rsi", "RSI", (_PERIOD,), lambda b, period: rsi(b["Close"], period), overlay=False),
    Operand("macd", "MACD line", (), lambda b: macd(b["Close"])[0], overlay=False),
    Operand("macd_signal", "MACD signal", (), lambda b: macd(b["Close"])[1], overlay=False),
    Operand("macd_hist", "MACD histogram", (), lambda b: macd(b["Close"])[2], overlay=False),
    Operand("adx", "ADX", (_PERIOD,), lambda b, period: adx(b, period)[0], overlay=False),
    Operand("plus_di", "+DI", (_PERIOD,), lambda b, period: adx(b, period)[1], overlay=False),
    Operand("minus_di", "−DI", (_PERIOD,), lambda b, period: adx(b, period)[2], overlay=False),
    Operand("stoch_k", "Stochastic %K", (_PERIOD,), lambda b, period: stochastic(b, period)[0], overlay=False),
    Operand("stoch_d", "Stochastic %D", (_PERIOD,), lambda b, period: stochastic(b, period)[1], overlay=False),
    Operand("cci", "CCI", (_p("period", "Period", 20, 5, 200),), cci, overlay=False),
    Operand("awesome", "Awesome Oscillator", (), awesome_oscillator, overlay=False),
    Operand("atr", "ATR", (_PERIOD,), atr, overlay=False),
    Operand("volume", "Volume", (), lambda b: b["Volume"].astype(float), overlay=False),
    Operand("volume_sma", "Average volume", (_p("period", "Bars", 20, 2, 300),),
            lambda b, period: sma(b["Volume"].astype(float), period).shift(1), overlay=False),
    # Time conditions, the way "Time of day is after 09:20" or "Day of week is one of Mon, Tue" read.
    Operand("time", "Time of day", (), time_of_day, overlay=False, intraday=True, kind="time"),
    Operand("weekday", "Day of week", (), day_of_week, overlay=False, kind="weekday"),
)}


def resolve_params(operand: Operand, given: dict | None) -> dict:
    given, out = dict(given or {}), {}
    for name, label, default, lo, hi, integer in operand.params:
        raw = given.pop(name, default)
        try:
            value = float(raw)
        except (TypeError, ValueError):
            raise ValueError(f"{operand.label}: {label} must be a number") from None
        if not np.isfinite(value) or not lo <= value <= hi:
            raise ValueError(f"{operand.label}: {label} must be between {lo:g} and {hi:g}")
        out[name] = int(round(value)) if integer else value
    if given:
        raise ValueError(f"{operand.label}: unknown setting {', '.join(sorted(given))}")
    return out


def compute(bars: pd.DataFrame, key: str, params: dict | None = None) -> pd.Series:
    if key not in OPERANDS:
        raise ValueError(f"unknown indicator: {key}")
    operand = OPERANDS[key]
    return operand.fn(bars, **resolve_params(operand, params))


def describe() -> list[dict]:
    return [{"key": o.key, "label": o.label, "overlay": o.overlay, "intraday": o.intraday, "kind": o.kind,
             "params": [{"name": n, "label": lab, "default": d, "minimum": lo, "maximum": hi, "integer": i}
                        for n, lab, d, lo, hi, i in o.params]} for o in OPERANDS.values()]
