"""Rule-based strategies: each maps daily bars to a target position per day.

A strategy returns a Series of 1 (long) or 0 (flat), where the value on day
``t`` uses bars up to and including ``t``. The engine acts on it at the next
day's open, so no strategy can trade on a bar it has not seen close.
Long-only: delivery (CNC) holdings cannot be shorted overnight.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from . import indicators as ind


@dataclass(frozen=True)
class Param:
    name: str
    label: str
    default: float
    minimum: float
    maximum: float
    integer: bool = True


@dataclass(frozen=True)
class Strategy:
    key: str
    name: str
    description: str
    params: tuple[Param, ...]
    fn: object
    intraday_only: bool = False
    source: str = ""
    style: str = ""                     # group in the picker: Legend, Trend, Breakout, Mean reversion, Swing, Scalping
    markets: tuple = ()                 # where it tends to work: bull, bear (when shorted in futures), sideways
    legend: str = ""                    # the trader whose rules it follows
    note: str = ""                      # how it behaves in each kind of market
    history_days: int = 0               # calendar days of intraday history a live algo must load (weekly pivots: 14)

    def resolve(self, given: dict | None) -> dict:
        """Parameter values, validated; raises ValueError naming the bad one."""
        given, out = dict(given or {}), {}
        for p in self.params:
            raw = given.pop(p.name, p.default)
            try:
                value = float(raw)
            except (TypeError, ValueError):
                raise ValueError(f"{p.label} must be a number") from None
            if not np.isfinite(value) or not p.minimum <= value <= p.maximum:
                raise ValueError(f"{p.label} must be between {p.minimum:g} and {p.maximum:g}")
            out[p.name] = int(round(value)) if p.integer else value
        if given:
            raise ValueError(f"unknown parameter(s) for {self.name}: {', '.join(sorted(given))}")
        return out

    def positions(self, bars: pd.DataFrame, params: dict | None = None) -> pd.Series:
        resolved = self.resolve(params)
        target = self.fn(bars, **resolved)
        return target.reindex(bars.index).fillna(0).astype(int).clip(0, 1)

    def describe(self) -> dict:
        return {"key": self.key, "name": self.name, "description": self.description,
                "params": [p.__dict__ for p in self.params], "intraday_only": self.intraday_only,
                "source": self.source, "custom": self.key.startswith("custom:"), "style": self.style or "Other",
                "markets": list(self.markets), "legend": self.legend, "note": self.note}


def _rsi(close: pd.Series, period: int) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi = 100 - 100 / (1 + rs)
    return rsi.where(loss != 0, 100.0).where(gain.notna())


def _atr(bars: pd.DataFrame, period: int) -> pd.Series:
    prev = bars["Close"].shift(1)
    tr = pd.concat([bars["High"] - bars["Low"], (bars["High"] - prev).abs(),
                    (bars["Low"] - prev).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def _latch(enter: pd.Series, leave: pd.Series) -> pd.Series:
    """Hold from an entry signal until an exit signal; exit wins a tie."""
    state, out = 0, np.zeros(len(enter), dtype=int)
    for i, (e, x) in enumerate(zip(enter.to_numpy(bool), leave.to_numpy(bool), strict=True)):
        if x:
            state = 0
        elif e:
            state = 1
        out[i] = state
    return pd.Series(out, index=enter.index)


def buy_hold(bars):
    return pd.Series(1, index=bars.index)


def sma_cross(bars, fast, slow):
    if fast >= slow:
        raise ValueError("Fast SMA must be shorter than slow SMA")
    close = bars["Close"]
    f, s = close.rolling(fast).mean(), close.rolling(slow).mean()
    return (f > s).where(s.notna(), False).astype(int)


def ema_cross(bars, fast, slow):
    if fast >= slow:
        raise ValueError("Fast EMA must be shorter than slow EMA")
    close = bars["Close"]
    f = close.ewm(span=fast, adjust=False, min_periods=fast).mean()
    s = close.ewm(span=slow, adjust=False, min_periods=slow).mean()
    return (f > s).where(s.notna(), False).astype(int)


def rsi_reversion(bars, period, buy_below, sell_above):
    if buy_below >= sell_above:
        raise ValueError("RSI buy level must be below the sell level")
    rsi = _rsi(bars["Close"], period)
    return _latch((rsi < buy_below).fillna(False), (rsi > sell_above).fillna(False))


def macd(bars, fast, slow, signal):
    if fast >= slow:
        raise ValueError("MACD fast period must be shorter than slow")
    close = bars["Close"]
    line = (close.ewm(span=fast, adjust=False, min_periods=fast).mean()
            - close.ewm(span=slow, adjust=False, min_periods=slow).mean())
    sig = line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    return (line > sig).where(sig.notna(), False).astype(int)


def donchian(bars, entry, exit):  # noqa: A002 - parameter name shown in the UI
    upper = bars["High"].rolling(entry).max().shift(1)
    lower = bars["Low"].rolling(exit).min().shift(1)
    return _latch((bars["Close"] > upper).fillna(False), (bars["Close"] < lower).fillna(False))


def supertrend(bars, period, multiplier):
    atr = _atr(bars, period)
    mid = (bars["High"] + bars["Low"]) / 2
    upper_basic, lower_basic = (mid + multiplier * atr).to_numpy(), (mid - multiplier * atr).to_numpy()
    close = bars["Close"].to_numpy()
    n = len(bars)
    upper, lower, trend = np.full(n, np.nan), np.full(n, np.nan), np.zeros(n, dtype=int)
    for i in range(n):
        if np.isnan(upper_basic[i]):
            continue
        if i == 0 or np.isnan(upper[i - 1]):
            upper[i], lower[i] = upper_basic[i], lower_basic[i]
            trend[i] = 1 if close[i] > upper[i] else 0
            continue
        upper[i] = upper_basic[i] if upper_basic[i] < upper[i - 1] or close[i - 1] > upper[i - 1] else upper[i - 1]
        lower[i] = lower_basic[i] if lower_basic[i] > lower[i - 1] or close[i - 1] < lower[i - 1] else lower[i - 1]
        if trend[i - 1] == 1:
            trend[i] = 0 if close[i] < lower[i] else 1
        else:
            trend[i] = 1 if close[i] > upper[i] else 0
    return pd.Series(trend, index=bars.index)


def bollinger_reversion(bars, period, width):
    close = bars["Close"]
    mid, sd = close.rolling(period).mean(), close.rolling(period).std(ddof=0)
    return _latch((close < mid - width * sd).fillna(False), (close > mid).fillna(False))


def heikin_ashi_trend(bars, confirm):
    ha = ind.heikin_ashi(bars)
    green = (ha["Close"] > ha["Open"]).astype(int)
    return _latch(green.rolling(confirm).sum() == confirm, green == 0)


def parabolic_sar(bars, step, maximum):
    return ind.parabolic_sar(bars, step, maximum)[1]


def awesome_oscillator(bars, fast, slow):
    if fast >= slow:
        raise ValueError("Awesome Oscillator fast period must be shorter than slow")
    ao = ind.awesome_oscillator(bars, fast, slow)
    return (ao > 0).where(ao.notna(), False).astype(int)


def dual_thrust(bars, lookback, k_up, k_down):
    """Buy above open + k·range, exit below open − k·range; range from the bars before."""
    hh, ll = bars["High"].rolling(lookback).max().shift(1), bars["Low"].rolling(lookback).min().shift(1)
    hc, lc = bars["Close"].rolling(lookback).max().shift(1), bars["Close"].rolling(lookback).min().shift(1)
    span = pd.concat([hh - lc, hc - ll], axis=1).max(axis=1)
    return _latch((bars["Close"] > bars["Open"] + k_up * span).fillna(False),
                  (bars["Close"] < bars["Open"] - k_down * span).fillna(False))


def bollinger_rsi(bars, period, width, buy_rsi, sell_rsi):
    if buy_rsi >= sell_rsi:
        raise ValueError("RSI buy level must be below the sell level")
    _, _, lower = ind.bollinger(bars["Close"], period, width)
    rsi = ind.rsi(bars["Close"], 14)
    return _latch(((bars["Close"] < lower) & (rsi < buy_rsi)).fillna(False), (rsi > sell_rsi).fillna(False))


def adx_trend(bars, period, threshold):
    adx, plus_di, minus_di = ind.adx(bars, period)
    return _latch(((adx > threshold) & (plus_di > minus_di)).fillna(False), (plus_di < minus_di).fillna(False))


def volume_breakout(bars, lookback, volume_multiple, exit):  # noqa: A002 - parameter name shown in the UI
    """Close above the prior N-bar high on volume several times its average."""
    prior_high, _ = ind.donchian(bars, lookback)
    average = bars["Volume"].astype(float).rolling(lookback).mean().shift(1)
    _, exit_low = ind.donchian(bars, exit)
    enter = (bars["Close"] > prior_high) & (bars["Volume"] >= volume_multiple * average)
    return _latch(enter.fillna(False), (bars["Close"] < exit_low).fillna(False))


def vwap_trend(bars, fast, slow):
    if fast >= slow:
        raise ValueError("Fast EMA must be shorter than slow EMA")
    close = bars["Close"]
    return ((close > ind.vwap(bars)) & (ind.ema(close, fast) > ind.ema(close, slow))).fillna(False).astype(int)


def opening_range_breakout(bars, minutes):
    """Long once price closes above the high of the day's first ``minutes``; out below its low."""
    day = bars.index.normalize()
    since_open = (bars.index - day - pd.Timedelta(hours=9, minutes=15)) / pd.Timedelta(minutes=1)
    opening = since_open < minutes
    range_high = bars["High"].where(opening).groupby(day).transform("max")
    range_low = bars["Low"].where(opening).groupby(day).transform("min")
    after = ~opening
    out = pd.Series(0, index=bars.index)
    for _, chunk in bars.groupby(day):
        idx = chunk.index
        out.loc[idx] = _latch((after[bars.index.isin(idx)] & (chunk["Close"] > range_high.loc[idx])).fillna(False),
                              (chunk["Close"] < range_low.loc[idx]).fillna(False)).to_numpy()
    return out


_QT = "je-suis-tm/quant-trading"
_FT = "freqtrade/freqtrade-strategies"

STRATEGIES: dict[str, Strategy] = {s.key: s for s in (
    Strategy("sma_cross", "SMA crossover", "Long while the fast average is above the slow one.",
             (Param("fast", "Fast SMA", 20, 2, 200), Param("slow", "Slow SMA", 50, 5, 400)), sma_cross),
    Strategy("ema_cross", "EMA crossover", "Long while the fast EMA is above the slow EMA.",
             (Param("fast", "Fast EMA", 12, 2, 200), Param("slow", "Slow EMA", 26, 5, 400)), ema_cross),
    Strategy("supertrend", "Supertrend", "Long while price holds above the ATR trailing band.",
             (Param("period", "ATR period", 10, 2, 100),
              Param("multiplier", "ATR multiplier", 3.0, 0.5, 10.0, integer=False)), supertrend),
    Strategy("rsi_reversion", "RSI mean reversion", "Buy when RSI is oversold, exit when it recovers.",
             (Param("period", "RSI period", 14, 2, 100), Param("buy_below", "Buy below", 30, 1, 60),
              Param("sell_above", "Sell above", 60, 30, 99)), rsi_reversion),
    Strategy("macd", "MACD", "Long while the MACD line is above its signal line.",
             (Param("fast", "Fast", 12, 2, 100), Param("slow", "Slow", 26, 5, 300),
              Param("signal", "Signal", 9, 2, 100)), macd),
    Strategy("donchian", "Donchian breakout", "Buy a new N-day high, exit on an M-day low.",
             (Param("entry", "Entry high (days)", 20, 2, 300), Param("exit", "Exit low (days)", 10, 2, 300)),
             donchian),
    Strategy("bollinger_reversion", "Bollinger reversion", "Buy below the lower band, exit at the mean.",
             (Param("period", "Period", 20, 5, 200),
              Param("width", "Band width (σ)", 2.0, 0.5, 5.0, integer=False)), bollinger_reversion),
    Strategy("volume_breakout", "Price-volume breakout", "Buy a close above the prior N-bar high on a volume surge; exit on an M-bar low.",
             (Param("lookback", "Lookback (bars)", 20, 5, 200),
              Param("volume_multiple", "Volume × average", 3.0, 1.0, 20.0, integer=False),
              Param("exit", "Exit low (bars)", 10, 2, 200)), volume_breakout, source="Zerodha Varsity algo video"),
    Strategy("heikin_ashi", "Heikin-Ashi trend", "Long after N green Heikin-Ashi candles in a row; out on the first red one.",
             (Param("confirm", "Green candles to enter", 2, 1, 10),), heikin_ashi_trend, source=_QT),
    Strategy("parabolic_sar", "Parabolic SAR", "Long while price is above the parabolic SAR.",
             (Param("step", "Step", 0.02, 0.005, 0.2, integer=False),
              Param("maximum", "Maximum", 0.2, 0.05, 1.0, integer=False)), parabolic_sar, source=_QT),
    Strategy("awesome_oscillator", "Awesome Oscillator", "Long while the Awesome Oscillator is above zero.",
             (Param("fast", "Fast", 5, 2, 50), Param("slow", "Slow", 34, 5, 200)), awesome_oscillator, source=_QT),
    Strategy("dual_thrust", "Dual Thrust", "Breakout above the open by a share of the recent range; exit on the opposite break.",
             (Param("lookback", "Range lookback (bars)", 4, 1, 60),
              Param("k_up", "Upper factor", 0.5, 0.1, 3.0, integer=False),
              Param("k_down", "Lower factor", 0.5, 0.1, 3.0, integer=False)), dual_thrust, source=_QT),
    Strategy("bollinger_rsi", "Bollinger + RSI", "Buy below the lower band while RSI is oversold; sell when RSI is overbought.",
             (Param("period", "Band period", 20, 5, 200), Param("width", "Band width (σ)", 2.0, 0.5, 5.0, integer=False),
              Param("buy_rsi", "Buy when RSI below", 30, 1, 60), Param("sell_rsi", "Sell when RSI above", 70, 30, 99)),
             bollinger_rsi, source=_FT),
    Strategy("adx_trend", "ADX trend", "Long when ADX shows a strong trend and +DI leads −DI; out when −DI leads.",
             (Param("period", "ADX period", 14, 2, 100), Param("threshold", "ADX above", 25, 5, 80)), adx_trend, source=_FT),
    Strategy("vwap_trend", "VWAP trend (intraday)", "Long while price is above the day's VWAP and the fast EMA leads the slow.",
             (Param("fast", "Fast EMA", 9, 2, 100), Param("slow", "Slow EMA", 21, 5, 300)), vwap_trend, intraday_only=True),
    Strategy("orb", "Opening range breakout (intraday)", "Buy a close above the high of the first minutes after 09:15; out below its low.",
             (Param("minutes", "Opening range (minutes)", 15, 5, 120),), opening_range_breakout, intraday_only=True),
    Strategy("buy_hold", "Buy and hold", "Buy on the first day and hold. The baseline.", (), buy_hold),
)}


def _register_more() -> None:
    from dataclasses import replace

    from . import legends as lg

    P = Param
    more = (
        Strategy("minervini", "Mark Minervini: Trend Template breakout",
                 "All eight Trend Template conditions (above the 50, 150 and 200-day averages in that order, the 200-day rising "
                 "for a month, 30%+ above the 52-week low, within 25% of the 52-week high), then a close above the recent pivot "
                 "high. Out on a close below the 50-day. Minervini also cuts losses at 7-8%: set the stoploss.",
                 (P("pivot", "Pivot high (days)", 20, 5, 120), P("exit_ma", "Exit below SMA", 50, 10, 200)), lg.minervini,
                 source="Trade Like a Stock Market Wizard (2013)"),
        Strategy("turtle", "Turtle Traders (Dennis & Eckhardt): Donchian + 2N stop",
                 "System 1: buy a 20-day high, sell a 10-day low; the stop is 2 × ATR(20) (\"2N\") below the entry. "
                 "System 2 is 55/20. Many small losses, a few large trend wins.",
                 (P("entry", "Entry high (days)", 20, 5, 120), P("exit", "Exit low (days)", 10, 2, 60),
                  P("atr_stop", "Stop, × ATR", 2.0, 0.5, 6.0, integer=False)), lg.turtle, source="Way of the Turtle (2007)"),
        Strategy("darvas", "Nicolas Darvas: box breakout",
                 "A tight trading box near the 52-week high, broken upward on rising volume. Out when price falls through "
                 "the box floor.",
                 (P("box_days", "Box length (days)", 20, 5, 60), P("max_box_pct", "Box height at most %", 12, 3, 40),
                  P("near_high_pct", "Within % of 52-week high", 10, 1, 30),
                  P("volume_multiple", "Volume × 50-day average", 1.5, 1.0, 5.0, integer=False)), lg.darvas,
                 source="How I Made $2,000,000 in the Stock Market (1960)"),
        Strategy("livermore", "Jesse Livermore: pivotal point breakout",
                 "Trade the line of least resistance: a new 50-day high, above the 200-day average, on volume. Out on a "
                 "10-day low.",
                 (P("pivot", "Pivot high (days)", 50, 10, 250), P("trend_ma", "Trend SMA", 200, 50, 300),
                  P("exit_low", "Exit low (days)", 10, 3, 60),
                  P("volume_multiple", "Volume × average", 1.5, 1.0, 5.0, integer=False)), lg.livermore,
                 source="How to Trade in Stocks (1940)"),
        Strategy("weinstein", "Stan Weinstein: Stage 2 breakout",
                 "Buy Stage 2: a breakout above resistance while price is over a rising 30-week (150-day) average, on volume. "
                 "Sell when it closes back under the average (Stage 3/4). Short Stage 4 in futures.",
                 (P("ma", "Average (days)", 150, 50, 250), P("slope_days", "Rising over (days)", 20, 5, 60),
                  P("breakout", "Breakout high (days)", 50, 10, 250),
                  P("volume_multiple", "Volume × average", 1.5, 1.0, 5.0, integer=False)), lg.weinstein,
                 source="Secrets for Profiting in Bull and Bear Markets (1988)"),
        Strategy("oneil", "William O'Neil: CAN SLIM base breakout",
                 "The chart part of CAN SLIM: a breakout from a 7-week base, within 5% of the 52-week high, on volume at least "
                 "1.5× average. Out below the 50-day; O'Neil's rule is to cut every loss at 7-8% (set the stoploss). "
                 "The earnings and market (C, A, M) checks are left to you.",
                 (P("base_days", "Base length (days)", 35, 10, 120), P("near_high_pct", "Within % of 52-week high", 5, 1, 25),
                  P("volume_multiple", "Volume × 50-day average", 1.5, 1.0, 5.0, integer=False),
                  P("exit_ma", "Exit below SMA", 50, 10, 200)), lg.oneil, source="How to Make Money in Stocks"),
        Strategy("connors_rsi2", "Larry Connors: RSI(2) pullback",
                 "Above the 200-day average, buy when the 2-period RSI is under 10; sell on a close above the 5-day average. "
                 "Short holds, high win rate, small average win.",
                 (P("trend_ma", "Trend SMA", 200, 50, 300), P("buy_below", "Buy when RSI(2) below", 10, 1, 40),
                  P("exit_ma", "Exit above SMA", 5, 2, 30)), lg.connors_rsi2, source="Short Term Trading Strategies That Work (2009)"),
        Strategy("holy_grail", "Linda Raschke: Holy Grail",
                 "In a strong trend (ADX above 30 and rising, +DI over −DI), wait for a pullback to the 20 EMA, then buy a "
                 "close above that bar's high. Out on a close under the EMA or when ADX drops below 20.",
                 (P("adx_period", "ADX period", 14, 5, 50), P("adx_min", "ADX above", 30, 15, 60),
                  P("ema_period", "EMA", 20, 5, 100)), lg.holy_grail, source="Street Smarts (1995)"),
        Strategy("williams_breakout", "Larry Williams: volatility breakout",
                 "A close above the open by 0.6 × yesterday's range shows a volatility expansion; hold a few days. On daily "
                 "candles it enters at the next open, later than Williams' intraday stop entry.",
                 (P("k", "× yesterday's range", 0.6, 0.1, 2.0, integer=False), P("hold", "Hold (days)", 3, 1, 20)),
                 lg.williams_breakout, source="Long-Term Secrets to Short-Term Trading (1999)"),
        Strategy("tudor_jones_200", "Paul Tudor Jones: 200-day rule",
                 "\"Nothing good happens below the 200-day moving average.\" Long above it, in cash below it. A band stops "
                 "whipsaws around the line.",
                 (P("ma", "Average (days)", 200, 50, 300), P("band_pct", "Band %", 1.0, 0.0, 5.0, integer=False)),
                 lg.tudor_jones_200, source="Money: Master the Game (2014), interview"),
        Strategy("bollinger_squeeze", "John Bollinger: the Squeeze",
                 "Band width near a 6-month low (volatility coiled), then a close above the upper band. Out at the middle band.",
                 (P("period", "Period", 20, 5, 100), P("width", "Band width (σ)", 2.0, 1.0, 4.0, integer=False),
                  P("lookback", "Squeeze lookback (bars)", 120, 20, 250)), lg.bollinger_squeeze,
                 source="Bollinger on Bollinger Bands (2001)"),
        Strategy("crabel_nr7", "Toby Crabel: NR7 breakout",
                 "The narrowest daily range of the last 7 days (a coil), then a close above that day's high. Out below its low "
                 "or after a few days.",
                 (P("hold", "Hold at most (days)", 5, 1, 30),), lg.crabel_nr7,
                 source="Day Trading with Short Term Price Patterns and Opening Range Breakout (1990)"),
        Strategy("buffett_fear", "Warren Buffett: greedy when others are fearful",
                 "Contrarian buying for indices and quality stocks: buy when the price is 20% below its 52-week high and "
                 "RSI(14) shows fear; hold until it is back near that high. Inspired by his words, not a system he ran: "
                 "Buffett picks businesses, not charts. See the Buffett screen on the Scanner tab.",
                 (P("drawdown_pct", "Down from 52-week high %", 20, 5, 60), P("rsi_below", "RSI(14) below", 35, 10, 60),
                  P("exit_within_pct", "Exit within % of the high", 2, 0, 20)), lg.buffett_fear,
                 source="1986 shareholder letter; NYT, Oct 2008"),
        Strategy("rsi2_vwap_scalp", "Scalp: RSI(2) dip above VWAP (intraday)",
                 "On 1-5 minute candles, above VWAP, buy when RSI(2) dips under 10; sell when it recovers above 70 or price "
                 "loses VWAP. Many small trades: charges and slippage decide whether it pays.",
                 (P("buy_below", "Buy when RSI(2) below", 10, 1, 40), P("sell_above", "Sell when RSI(2) above", 70, 50, 99)),
                 lg.rsi2_vwap_scalp, intraday_only=True),
        Strategy("vwap_bounce", "Scalp: VWAP bounce (intraday)",
                 "A dip that touches VWAP and closes back above it while the EMA is rising; out on a close below VWAP.",
                 (P("ema_period", "EMA", 20, 5, 100),), lg.vwap_bounce, intraday_only=True),
        Strategy("pullback_50", "Swing: pullback to the 50-day",
                 "In an uptrend (50-day over 200-day), buy a dip to the 50-day once RSI has cooled; take profit at a new "
                 "20-day high, cut it 3% under the 50-day.",
                 (P("near_pct", "Low within % of SMA 50", 2.0, 0.0, 10.0, integer=False), P("rsi_below", "RSI(14) below", 50, 20, 70)),
                 lg.pullback_50),
        Strategy("inside_bar", "Swing: inside-bar breakout",
                 "An inside day (its range inside the day before), then a close above the mother bar's high within 3 days. "
                 "Out below the mother bar's low or after the holding period.",
                 (P("hold", "Hold at most (days)", 10, 1, 40),), lg.inside_bar),
    )
    meta = {
        # key: (style, markets, legend, note)
        "minervini": ("Legend", ("bull",), "Mark Minervini", "Bull: its home. Sideways: few setups, some failed breakouts. Bear: no stock passes the template, so it sits in cash."),
        "turtle": ("Legend", ("bull", "bear"), "Richard Dennis", "Strong trends either way (short the breakdowns in futures). Sideways: repeated small losses."),
        "darvas": ("Legend", ("bull",), "Nicolas Darvas", "Bull markets with leaders making new highs. Bear and sideways: few boxes break, it waits."),
        "livermore": ("Legend", ("bull", "bear"), "Jesse Livermore", "Trending markets. Sideways ranges cause false pivots."),
        "weinstein": ("Legend", ("bull", "bear"), "Stan Weinstein", "Stage 2 (bull) long; Stage 4 (bear) short in futures. Stage 1 and 3 (sideways): stay out."),
        "oneil": ("Legend", ("bull",), "William O'Neil", "Confirmed uptrends only; O'Neil says 3 of 4 stocks follow the market, so stand aside in corrections."),
        "connors_rsi2": ("Legend", ("bull", "sideways"), "Larry Connors", "Pullbacks in a bull market and ranges. Bear: the 200-day filter keeps it out."),
        "holy_grail": ("Legend", ("bull", "bear"), "Linda Raschke", "Strong trends with orderly pullbacks. Choppy markets: ADX stays low, so few trades."),
        "williams_breakout": ("Legend", ("bull", "sideways"), "Larry Williams", "Volatile, two-way markets; short holds."),
        "tudor_jones_200": ("Legend", ("bull",), "Paul Tudor Jones", "Rides bull markets; steps aside before most of a bear market. Sideways around the average: whipsaws."),
        "bollinger_squeeze": ("Legend", ("sideways", "bull"), "John Bollinger", "Quiet ranges that are about to break out."),
        "crabel_nr7": ("Legend", ("sideways", "bull"), "Toby Crabel", "Coiling markets before an expansion."),
        "buffett_fear": ("Legend", ("bear",), "Warren Buffett", "Buys during bear markets and crashes, sells in the recovery. Idle in steady bull markets."),
        "rsi2_vwap_scalp": ("Scalping", ("sideways", "bull"), "", "Intraday ranges with an up bias."),
        "vwap_bounce": ("Scalping", ("bull",), "", "Intraday up-trending days."),
        "vwap_trend": ("Scalping", ("bull",), "", "Intraday trend days. Choppy days: many small losses."),
        "orb": ("Scalping", ("bull",), "Toby Crabel", "Days that trend from the open. Range days: false breaks."),
        "pullback_50": ("Swing", ("bull",), "", "Healthy bull markets with pullbacks."),
        "inside_bar": ("Swing", ("bull", "sideways"), "", "After pauses inside a trend or range."),
        "sma_cross": ("Trend", ("bull",), "", "Long trends; whipsaws in ranges; in cash in bear markets (short in futures)."),
        "ema_cross": ("Trend", ("bull",), "", "Like the SMA cross, a little faster."),
        "supertrend": ("Trend", ("bull", "bear"), "Olivier Seban", "Clear trends either way; sideways markets flip it often."),
        "macd": ("Trend", ("bull",), "Gerald Appel", "Trends; lags at turns."),
        "donchian": ("Breakout", ("bull", "bear"), "Richard Donchian", "Strong trends; the father of trend following."),
        "adx_trend": ("Trend", ("bull", "bear"), "J. Welles Wilder", "Only trades when ADX says there is a trend."),
        "parabolic_sar": ("Trend", ("bull", "bear"), "J. Welles Wilder", "Steady trends; very poor in ranges."),
        "heikin_ashi": ("Trend", ("bull",), "", "Smooth trends."),
        "awesome_oscillator": ("Trend", ("bull",), "Bill Williams", "Momentum phases."),
        "dual_thrust": ("Breakout", ("bull", "sideways"), "Michael Chalek", "Range expansions."),
        "volume_breakout": ("Breakout", ("bull",), "", "Breakouts with participation."),
        "rsi_reversion": ("Mean reversion", ("sideways",), "", "Ranges. In a falling market it buys too early."),
        "bollinger_reversion": ("Mean reversion", ("sideways",), "John Bollinger", "Ranges; dangerous when a trend starts."),
        "bollinger_rsi": ("Mean reversion", ("sideways",), "", "Ranges."),
        "buy_hold": ("Baseline", ("bull",), "", "The benchmark to beat."),
    }
    for s in more:
        STRATEGIES[s.key] = s
    for key, (style, markets, legend, note) in meta.items():
        if key in STRATEGIES:
            STRATEGIES[key] = replace(STRATEGIES[key], style=style, markets=markets, legend=legend, note=note)


_register_more()
