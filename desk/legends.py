"""Strategies after named traders, plus scalping and swing set-ups.

Each follows the trader's own published rules as closely as daily (or
intraday) candles of a single instrument allow, and says where it departs.
Like every strategy here it is long-only: 1 = hold, 0 = flat. The futures
backtest turns "flat" into a short when asked, which is how these rules are
traded in a falling market.

Sources (the books and papers the rules come from):

- Mark Minervini, *Trade Like a Stock Market Wizard* (2013): the Trend Template.
- Richard Dennis and William Eckhardt's Turtle rules, as published by Curtis Faith, *Way of the Turtle* (2007).
- Nicolas Darvas, *How I Made $2,000,000 in the Stock Market* (1960): the box.
- Jesse Livermore, *How to Trade in Stocks* (1940): pivotal points.
- Stan Weinstein, *Secrets for Profiting in Bull and Bear Markets* (1988): stage analysis, 30-week average.
- William O'Neil, *How to Make Money in Stocks* (CAN SLIM): base breakout near highs on volume, 7-8% stop.
- Larry Connors and Cesar Alvarez, *Short Term Trading Strategies That Work* (2009): RSI(2).
- Linda Raschke and Larry Connors, *Street Smarts* (1995): the Holy Grail.
- Larry Williams, *Long-Term Secrets to Short-Term Trading* (1999): volatility breakout.
- Paul Tudor Jones: the 200-day moving average rule (interview in Tony Robbins, *Money: Master the Game*, 2014).
- John Bollinger, *Bollinger on Bollinger Bands* (2001): the Squeeze.
- Toby Crabel, *Day Trading with Short Term Price Patterns and Opening Range Breakout* (1990): NR7.
- Warren Buffett, "be greedy when others are fearful" (1986 shareholder letter; NYT op-ed, 2008).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from . import indicators as ind


def _latch(enter, leave):
    from .strategies import _latch as latch
    return latch(enter.fillna(False), leave.fillna(False))


def _volume_ok(bars, multiple, window=50):
    """Volume at least ``multiple`` × its average. Indices often carry no volume: then the test is skipped."""
    volume = bars["Volume"].astype(float)
    average = volume.rolling(window, min_periods=10).mean().shift(1)
    return (volume >= multiple * average) | (average.fillna(0) <= 0)


def _sma(series, n):
    return series.rolling(n).mean()


def _hold_loop(enter: pd.Series, exit_rule, hold: int = 0) -> pd.Series:
    """Hold from ``enter`` until ``exit_rule(i, entry_index)`` is true, or ``hold`` bars have passed (0 = no limit)."""
    signal, out, state, start = enter.fillna(False).to_numpy(bool), np.zeros(len(enter), dtype=int), 0, 0
    for i in range(len(signal)):
        if state and (exit_rule(i, start) or (hold and i - start >= hold)):
            state = 0
        elif not state and signal[i]:
            state, start = 1, i
        out[i] = state
    return pd.Series(out, index=enter.index)


# -- legends ---------------------------------------------------------------------

def minervini(bars, pivot, exit_ma):
    """Trend Template, all eight conditions, then a close above the last ``pivot``-day high; out below the 50-day."""
    c, h, lo = bars["Close"], bars["High"], bars["Low"]
    s50, s150, s200 = _sma(c, 50), _sma(c, 150), _sma(c, 200)
    hi52, lo52 = h.rolling(250, min_periods=200).max(), lo.rolling(250, min_periods=200).min()
    template = ((c > s150) & (c > s200) & (s150 > s200) & (s200 > s200.shift(21)) & (s50 > s150) & (c > s50)
                & (c >= 1.30 * lo52) & (c >= 0.75 * hi52))
    breakout = c > h.rolling(pivot).max().shift(1)
    return _latch(template & breakout, c < _sma(c, exit_ma))


def turtle(bars, entry, exit, atr_stop):  # noqa: A002 - parameter name shown in the UI
    """Buy an ``entry``-day high; sell an ``exit``-day low or a fall of ``atr_stop`` × ATR(20) from the entry."""
    c = bars["Close"].to_numpy(float)
    upper = bars["High"].rolling(entry).max().shift(1)
    lower = bars["Low"].rolling(exit).min().shift(1).to_numpy(float)
    atr = ind.atr(bars, 20).to_numpy(float)
    return _hold_loop(bars["Close"] > upper,
                      lambda i, s: c[i] < lower[i] or (not np.isnan(atr[s]) and c[i] < c[s] - atr_stop * atr[s]))


def darvas(bars, box_days, max_box_pct, near_high_pct, volume_multiple):
    """A tight box near the 52-week high, broken upward on volume; out when price falls through the box floor."""
    c, h, lo = bars["Close"], bars["High"], bars["Low"]
    top, bottom = h.rolling(box_days).max().shift(1), lo.rolling(box_days).min().shift(1)
    tight = (top / bottom - 1) * 100 <= max_box_pct
    near = c >= (1 - near_high_pct / 100) * h.rolling(250, min_periods=120).max()
    return _latch((c > top) & tight & near & _volume_ok(bars, volume_multiple), c < bottom)


def livermore(bars, pivot, trend_ma, exit_low, volume_multiple):
    """A new ``pivot``-day high in a market above its long average, on volume; out on an ``exit_low``-day low."""
    c = bars["Close"]
    enter = (c > bars["High"].rolling(pivot).max().shift(1)) & (c > _sma(c, trend_ma)) & _volume_ok(bars, volume_multiple)
    return _latch(enter, c < bars["Low"].rolling(exit_low).min().shift(1))


def weinstein(bars, ma, slope_days, breakout, volume_multiple):
    """Stage 2: above a rising 30-week (150-day) average, breaking out on volume. Out when it closes back below."""
    c = bars["Close"]
    s = _sma(c, ma)
    enter = (c > s) & (s > s.shift(slope_days)) & (c > bars["High"].rolling(breakout).max().shift(1)) \
        & _volume_ok(bars, volume_multiple)
    return _latch(enter, c < s)


def oneil(bars, base_days, near_high_pct, volume_multiple, exit_ma):
    """Breakout from a ``base_days`` base within ``near_high_pct``% of the 52-week high, volume ≥ multiple × average."""
    c, h = bars["Close"], bars["High"]
    near = c >= (1 - near_high_pct / 100) * h.rolling(250, min_periods=120).max()
    enter = (c > h.rolling(base_days).max().shift(1)) & near & _volume_ok(bars, volume_multiple)
    return _latch(enter, c < _sma(c, exit_ma))


def connors_rsi2(bars, trend_ma, buy_below, exit_ma):
    """Above the 200-day, buy when 2-period RSI is deeply oversold; sell on a close above the 5-day average."""
    c = bars["Close"]
    return _latch((c > _sma(c, trend_ma)) & (ind.rsi(c, 2) < buy_below), c > _sma(c, exit_ma))


def holy_grail(bars, adx_period, adx_min, ema_period):
    """ADX above ``adx_min`` and rising, +DI leading: a pullback that touches the 20 EMA, then a close above that
    bar's high. Out when price closes below the EMA or the trend fades (ADX under 20)."""
    c, h, lo = bars["Close"], bars["High"], bars["Low"]
    adx, plus_di, minus_di = ind.adx(bars, adx_period)
    e = ind.ema(c, ema_period)
    strong = (adx > adx_min) & (adx > adx.shift(3)) & (plus_di > minus_di)
    touched = (lo <= e) & strong
    enter = touched.shift(1, fill_value=False) & (c > h.shift(1))
    return _latch(enter, (c < e) | (adx < 20))


def williams_breakout(bars, k, hold):
    """A close above the open by ``k`` × yesterday's range (a volatility expansion); held ``hold`` days."""
    rng = (bars["High"] - bars["Low"]).shift(1)
    close = bars["Close"].to_numpy(float)
    opens = bars["Open"].to_numpy(float)
    return _hold_loop(bars["Close"] > bars["Open"] + k * rng, lambda i, s: close[i] < opens[i] and i > s, hold)


def tudor_jones_200(bars, ma, band_pct):
    """Long above the 200-day average (by ``band_pct``%), out below it."""
    c = bars["Close"]
    s = _sma(c, ma)
    return _latch(c > s * (1 + band_pct / 100), c < s * (1 - band_pct / 100))


def bollinger_squeeze(bars, period, width, lookback):
    """Band width at (near) its ``lookback``-bar low in the last 5 bars, then a close above the upper band."""
    c = bars["Close"]
    upper, mid, lower = ind.bollinger(c, period, width)
    bandwidth = (upper - lower) / mid
    squeezed = (bandwidth <= bandwidth.rolling(lookback, min_periods=lookback // 2).min() * 1.1)
    return _latch((c > upper) & squeezed.rolling(5).max().astype(bool), c < mid)


def crabel_nr7(bars, hold):
    """The narrowest range of 7 days, then a close above that day's high; out below its low or after ``hold`` days."""
    h, lo, c = bars["High"], bars["Low"], bars["Close"]
    rng = h - lo
    nr7 = (rng == rng.rolling(7).min()).shift(1, fill_value=False)
    mother_low = lo.shift(1).to_numpy(float)
    close = c.to_numpy(float)
    return _hold_loop(nr7 & (c > h.shift(1)), lambda i, s: close[i] < mother_low[s], hold)


def buffett_fear(bars, drawdown_pct, rsi_below, exit_within_pct):
    """Buy when the market is down ``drawdown_pct``% from its 52-week high and RSI(14) shows fear; hold until it is
    back within ``exit_within_pct``% of that high."""
    c = bars["Close"]
    high = bars["High"].rolling(250, min_periods=120).max()
    enter = (c <= high * (1 - drawdown_pct / 100)) & (ind.rsi(c, 14) < rsi_below)
    return _latch(enter, c >= high * (1 - exit_within_pct / 100))


# -- scalping and swing --------------------------------------------------------------

def rsi2_vwap_scalp(bars, buy_below, sell_above):
    """Intraday: above VWAP, buy a 2-period RSI dip; sell when RSI(2) recovers or price loses VWAP."""
    c = bars["Close"]
    vwap, rsi = ind.vwap(bars), ind.rsi(c, 2)
    return _latch((c > vwap) & (rsi < buy_below), (rsi > sell_above) | (c < vwap))


def vwap_bounce(bars, ema_period):
    """Intraday: a dip that touches VWAP and closes back above it while the EMA rises; out on a close below VWAP."""
    c = bars["Close"]
    vwap, e = ind.vwap(bars), ind.ema(c, ema_period)
    return _latch((bars["Low"] <= vwap) & (c > vwap) & (e > e.shift(3)), c < vwap)


def pullback_50(bars, near_pct, rsi_below):
    """Swing: in an uptrend (50 > 200-day), a pullback to the 50-day with RSI cooled; out at a new 20-day high
    (the swing target) or 3% below the 50-day (the trend has broken)."""
    c = bars["Close"]
    s50, s200 = _sma(c, 50), _sma(c, 200)
    enter = (c > s200) & (s50 > s200) & (bars["Low"] <= s50 * (1 + near_pct / 100)) & (c > s50) & (ind.rsi(c, 14) < rsi_below)
    return _latch(enter, (c > bars["High"].rolling(20).max().shift(1)) | (c < s50 * 0.97))


def inside_bar(bars, hold):
    """Swing: an inside day (range inside the day before), then a close above the mother bar's high within 3 days;
    out below the mother bar's low or after ``hold`` days."""
    h, lo, c = bars["High"], bars["Low"], bars["Close"]
    inside = (h < h.shift(1)) & (lo > lo.shift(1))
    mother_high = h.shift(1).where(inside).ffill(limit=3)
    mother_low = lo.shift(1).where(inside).ffill(limit=3)
    floor = mother_low.to_numpy(float)
    close = c.to_numpy(float)
    return _hold_loop((c > mother_high) & ~inside, lambda i, s: close[i] < floor[s], hold)
