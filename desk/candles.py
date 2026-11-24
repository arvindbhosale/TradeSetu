"""Candle patterns, what followed them on the same stock, and a range of likely prices ahead.

Nothing here says "this pattern means up". Each pattern is a shape rule. What it is worth is measured:
for every past time the shape closed, where was the price 1, 5 or 10 candles later, against an ordinary
candle on the same stock (a stock that rose 54% of all days makes a "56% up" pattern worth nothing).

Rules, the same in the browser (``static/chartdesk/patterns.js``, checked against this file by a test):

- A pattern is known only when its last candle has **closed**, and uses that candle and the ones before it.
  The move after it is measured from that close.
- ``body`` is |close - open|, ``range`` high - low, ``upper`` and ``lower`` the wicks. ``avg_body`` and
  ``avg_range`` are the means of the 10 candles before (not including) the candle being judged.
- "Fell before" / "rose before": the close of the candle before the pattern against the close five
  candles earlier. It tells a Hammer (after a fall) from a Hanging Man (same shape after a rise).

Filters (each optional), on the candle the pattern closes on:

- Trend before it, on the candle before: Uptrend when that close is above its 50-candle average and the
  20-candle average is above the 50; Downtrend when both are below (the desk's default trend rule).
- Near a recent low (high): the lowest low (highest high) of the pattern's last three candles is within
  ``near`` x ATR(14) of the lowest low (highest high) of the 20 candles before them, or beyond it.
- Above-average volume: volume above the mean of the 20 candles before.

The range ahead is "filtered historical simulation": every past N-candle move divided by the volatility
of its time (standard deviation of the 20 one-candle log returns before it, times the square root of N),
then scaled by today's volatility. It is checked on the newest third of the data with quantiles taken
from the older two-thirds only, so the "held the price" figure is out of sample.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from . import indicators as ind

# key: (name, signal, candles, what it looks like). signal: up / down / neutral, the textbook reading only.
PATTERNS: dict[str, tuple[str, str, int, str]] = {
    "doji": ("Doji", "neutral", 1, "Open and close almost equal: buyers and sellers ended level."),
    "hammer": ("Hammer", "up", 1, "After a fall: small body at the top, a lower wick at least twice the body."),
    "inverted_hammer": ("Inverted Hammer", "up", 1, "After a fall: small body at the bottom, a long upper wick."),
    "hanging_man": ("Hanging Man", "down", 1, "After a rise: the Hammer's shape, small body on a long lower wick."),
    "shooting_star": ("Shooting Star", "down", 1, "After a rise: small body at the bottom, a long upper wick."),
    "bullish_marubozu": ("Bullish Marubozu", "up", 1, "A large green candle with almost no wicks."),
    "bearish_marubozu": ("Bearish Marubozu", "down", 1, "A large red candle with almost no wicks."),
    "bullish_engulfing": ("Bullish Engulfing", "up", 2, "A green body that covers the whole red body before it."),
    "bearish_engulfing": ("Bearish Engulfing", "down", 2, "A red body that covers the whole green body before it."),
    "bullish_harami": ("Bullish Harami", "up", 2, "A small green body inside a large red body before it."),
    "bearish_harami": ("Bearish Harami", "down", 2, "A small red body inside a large green body before it."),
    "piercing_line": ("Piercing Line", "up", 2, "Opens below a large red candle's close, closes above its middle."),
    "dark_cloud_cover": ("Dark Cloud Cover", "down", 2, "Opens above a large green candle's close, closes below its middle."),
    "tweezer_bottom": ("Tweezer Bottom", "up", 2, "After a fall: a red then a green candle with the same low."),
    "tweezer_top": ("Tweezer Top", "down", 2, "After a rise: a green then a red candle with the same high."),
    "morning_star": ("Morning Star", "up", 3, "A large red candle, a small candle at or below its close, then a green candle closing above the red one's middle."),
    "evening_star": ("Evening Star", "down", 3, "A large green candle, a small candle at or above its close, then a red candle closing below the green one's middle."),
    "three_white_soldiers": ("Three White Soldiers", "up", 3, "Three green candles, each opening inside the one before and closing higher, with small upper wicks."),
    "three_black_crows": ("Three Black Crows", "down", 3, "Three red candles, each opening inside the one before and closing lower, with small lower wicks."),
}

AVG = 10            # candles in the average body and range
TREND_BACK = 5      # "fell before": close one candle back against close this many further back
FAST, SLOW = 20, 50  # trend averages (the desk's default trend rule)
LEVEL_BARS = 20     # recent high or low: over this many candles before the pattern's last three
VOLUME_BARS = 20
ATR_BARS = 14
HORIZONS = (1, 5, 10)
MIN_CASES = 30
TRENDS = ("any", "up", "down")
PLACES = ("any", "low", "high")


def _prior_mean(values: pd.Series, n: int) -> pd.Series:
    """Mean of the ``n`` values before each row (not the row itself)."""
    return values.rolling(n).mean().shift(1)


def detect(bars: pd.DataFrame) -> pd.DataFrame:
    """One True/False column per pattern: True on the candle the pattern closes on."""
    o, h, low, c = (bars[k].astype(float) for k in ("Open", "High", "Low", "Close"))
    body, rng = (c - o).abs(), h - low
    upper, lower = h - np.maximum(o, c), np.minimum(o, c) - low
    green, red = c > o, c < o
    avg_body, avg_range = _prior_mean(body, AVG), _prior_mean(rng, AVG)
    fell = c.shift(1) < c.shift(1 + TREND_BACK)
    rose = c.shift(1) > c.shift(1 + TREND_BACK)
    ok = (rng > 0) & avg_body.notna() & c.shift(1 + TREND_BACK).notna()

    def p(k: int, s: pd.Series) -> pd.Series:      # the value k candles back (False / NaN before the first)
        return s.shift(k, fill_value=False) if s.dtype == bool else s.shift(k)

    top, bottom = np.maximum(o, c), np.minimum(o, c)
    small_low = (lower >= 2 * body) & (upper <= 0.15 * rng) & (body > 0.02 * rng)
    small_high = (upper >= 2 * body) & (lower <= 0.15 * rng) & (body > 0.02 * rng)
    long_prev = p(1, body) >= p(1, avg_body)
    out = {
        "doji": body <= 0.1 * rng,
        "hammer": small_low & fell,
        "inverted_hammer": small_high & fell,
        "hanging_man": small_low & rose,
        "shooting_star": small_high & rose,
        "bullish_marubozu": green & (body >= 0.9 * rng) & (body >= 1.3 * avg_body),
        "bearish_marubozu": red & (body >= 0.9 * rng) & (body >= 1.3 * avg_body),
        "bullish_engulfing": p(1, red) & green & (o <= p(1, c)) & (c >= p(1, o)) & (body > p(1, body)),
        "bearish_engulfing": p(1, green) & red & (o >= p(1, c)) & (c <= p(1, o)) & (body > p(1, body)),
        "bullish_harami": p(1, red) & long_prev & green & (top <= p(1, o)) & (bottom >= p(1, c)) & (body <= 0.5 * p(1, body)),
        "bearish_harami": p(1, green) & long_prev & red & (top <= p(1, c)) & (bottom >= p(1, o)) & (body <= 0.5 * p(1, body)),
        "piercing_line": p(1, red) & long_prev & green & (o < p(1, c)) & (c > (p(1, o) + p(1, c)) / 2) & (c < p(1, o)),
        "dark_cloud_cover": p(1, green) & long_prev & red & (o > p(1, c)) & (c < (p(1, o) + p(1, c)) / 2) & (c > p(1, o)),
        "tweezer_bottom": p(1, red) & green & ((low - p(1, low)).abs() <= 0.05 * avg_range) & fell.shift(1, fill_value=False),
        "tweezer_top": p(1, green) & red & ((h - p(1, h)).abs() <= 0.05 * avg_range) & rose.shift(1, fill_value=False),
        "morning_star": (p(2, red) & (p(2, body) >= p(2, avg_body)) & (p(1, body) <= 0.5 * p(2, avg_body))
                         & (p(1, top) <= p(2, c)) & green & (c > (p(2, o) + p(2, c)) / 2)),
        "evening_star": (p(2, green) & (p(2, body) >= p(2, avg_body)) & (p(1, body) <= 0.5 * p(2, avg_body))
                         & (p(1, bottom) >= p(2, c)) & red & (c < (p(2, o) + p(2, c)) / 2)),
    }
    soldiers = green & (upper <= 0.5 * body) & (body >= 0.5 * avg_body) & (c > p(1, c)) & (o > p(1, o)) & (o < p(1, c))
    crows = red & (lower <= 0.5 * body) & (body >= 0.5 * avg_body) & (c < p(1, c)) & (o < p(1, o)) & (o > p(1, c))
    out["three_white_soldiers"] = soldiers & p(1, soldiers) & p(2, green) & (p(2, upper) <= 0.5 * p(2, body)) & (p(2, body) >= 0.5 * p(2, avg_body))
    out["three_black_crows"] = crows & p(1, crows) & p(2, red) & (p(2, lower) <= 0.5 * p(2, body)) & (p(2, body) >= 0.5 * p(2, avg_body))
    frame = pd.DataFrame({k: out[k] for k in PATTERNS}, index=bars.index)
    need = {k: ok & ok.shift(PATTERNS[k][2] - 1, fill_value=False) for k in PATTERNS}
    return pd.DataFrame({k: (frame[k].fillna(False).astype(bool) & need[k]) for k in PATTERNS}, index=bars.index)


def context(bars: pd.DataFrame, near: float = 1.0) -> pd.DataFrame:
    """The filters on each candle: ``trend`` up/down/side (on the candle before), ``near_low``, ``near_high``
    and ``high_volume`` (True/False; NaN-free)."""
    c = bars["Close"].astype(float)
    fast, slow = ind.sma(c, FAST), ind.sma(c, SLOW)
    up = (c > slow) & (fast > slow)
    down = (c < slow) & (fast < slow)
    trend = pd.Series(np.where(up, "up", np.where(down, "down", "side")), index=bars.index).where(slow.notna(), "")
    trend = trend.shift(1).fillna("")
    atr = ind.atr(bars, ATR_BARS)
    last3_low, last3_high = bars["Low"].rolling(3).min(), bars["High"].rolling(3).max()
    ref_low, ref_high = bars["Low"].rolling(LEVEL_BARS).min().shift(3), bars["High"].rolling(LEVEL_BARS).max().shift(3)
    near_low = (last3_low - ref_low <= near * atr) & ref_low.notna() & atr.notna()
    near_high = (ref_high - last3_high <= near * atr) & ref_high.notna() & atr.notna()
    vol = bars["Volume"].astype(float) if "Volume" in bars else pd.Series(0.0, index=bars.index)
    avg_vol = _prior_mean(vol, VOLUME_BARS)
    high_volume = (vol > avg_vol) & (avg_vol > 0)
    return pd.DataFrame({"trend": trend, "near_low": near_low.fillna(False), "near_high": near_high.fillna(False),
                         "high_volume": high_volume.fillna(False)}, index=bars.index)


def clean_filters(raw: dict | None) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    trend = raw.get("trend", "any")
    place = raw.get("place", "any")
    if trend not in TRENDS:
        raise ValueError("Trend before it must be any, up or down")
    if place not in PLACES:
        raise ValueError("Where must be any, low or high")
    near = raw.get("near", 1.0)
    if isinstance(near, bool) or not isinstance(near, (int, float)) or not 0.1 <= near <= 5:
        raise ValueError("Near means within 0.1 to 5 times the average candle range (ATR)")
    return {"trend": trend, "place": place, "volume": bool(raw.get("volume", False)), "near": float(near)}


def _mask(ctx: pd.DataFrame, f: dict) -> pd.Series:
    m = pd.Series(True, index=ctx.index)
    if f["trend"] != "any":
        m &= ctx["trend"] == f["trend"]
    if f["place"] == "low":
        m &= ctx["near_low"]
    elif f["place"] == "high":
        m &= ctx["near_high"]
    if f["volume"]:
        m &= ctx["high_volume"]
    return m


def _round(v: float, dp: int) -> float:
    """Half up, the browser's Math.round, so both show the same figure."""
    return math.floor(v * 10 ** dp + 0.5) / 10 ** dp


def _stats(moves: np.ndarray) -> dict:
    if not len(moves):
        return {"cases": 0, "up_pct": None, "avg_pct": None, "median_pct": None, "worst_pct": None, "best_pct": None}
    return {"cases": int(len(moves)), "up_pct": _round(float((moves > 0).sum()) / len(moves) * 100, 1),
            "avg_pct": _round(float(moves.sum()) / len(moves) * 100, 2), "median_pct": _round(float(np.median(moves)) * 100, 2),
            "worst_pct": _round(float(moves.min()) * 100, 2), "best_pct": _round(float(moves.max()) * 100, 2)}


def verdict(cases: int, up_pct: float | None, normal_up_pct: float | None, min_cases: int = MIN_CASES) -> dict:
    """Plain words for one pattern against the normal candle. ``z`` is how many standard errors apart the two
    "price higher" shares are. A chart tests 19 patterns at 3 look-aheads, so a z of 2 turns up by luck a few
    times on pure noise; only 3 or more is called a difference, 2 to 3 is "could be luck"."""
    if cases < min_cases or up_pct is None or normal_up_pct is None:
        return {"key": "few", "text": "Too few cases", "z": None}
    p0 = min(max(normal_up_pct / 100, 0.01), 0.99)
    z = (up_pct / 100 - p0) / math.sqrt(p0 * (1 - p0) / cases)
    if z >= 3:
        return {"key": "up", "text": "Up more often than normal", "z": _round(z, 2)}
    if z <= -3:
        return {"key": "down", "text": "Down more often than normal", "z": _round(z, 2)}
    if z >= 2:
        return {"key": "maybe_up", "text": "A little more up, could be luck", "z": _round(z, 2)}
    if z <= -2:
        return {"key": "maybe_down", "text": "A little more down, could be luck", "z": _round(z, 2)}
    return {"key": "none", "text": "No clear edge", "z": _round(z, 2)}


def odds(bars: pd.DataFrame, *, closed: int | None = None, filters: dict | None = None,
         horizons: tuple[int, ...] = HORIZONS, min_cases: int = MIN_CASES) -> dict:
    """What followed each pattern on these bars. ``closed`` is how many of the bars have closed (the rest
    are ignored, so a forming candle never counts)."""
    f = clean_filters(filters)
    bars = bars.iloc[: len(bars) if closed is None else closed]
    found, ctx = detect(bars), context(bars, f["near"])
    mask = _mask(ctx, f).to_numpy()
    close = bars["Close"].astype(float).to_numpy()
    n = len(close)
    out, normal = {}, {}
    for hz in horizons:
        moves = np.full(n, np.nan)
        if n > hz:
            moves[: n - hz] = close[hz:] / close[: n - hz] - 1
        valid = ~np.isnan(moves)
        normal[hz] = _stats(moves[valid & mask])
        for key in PATTERNS:
            hit = found[key].to_numpy() & mask
            row = out.setdefault(key, {"key": key, "name": PATTERNS[key][0], "signal": PATTERNS[key][1],
                                       "seen": int(hit.sum()), "after": {}})
            s = _stats(moves[hit & valid])
            s["verdict"] = verdict(s["cases"], s["up_pct"], normal[hz]["up_pct"], min_cases)
            row["after"][str(hz)] = s
    return {"patterns": list(out.values()), "normal": {str(k): v for k, v in normal.items()}, "filters": f,
            "bars": n, "tested": len(PATTERNS) * len(horizons)}


def pooled(frames: dict[str, pd.DataFrame], *, filters: dict | None = None, horizons: tuple[int, ...] = HORIZONS,
           min_cases: int = MIN_CASES) -> dict:
    """The same odds with every stock's cases added together (each stock's moves stay its own, so no move
    crosses from one stock to another). The normal candle is pooled the same way."""
    f = clean_filters(filters)
    pat: dict[str, dict[int, list]] = {k: {hz: [] for hz in horizons} for k in PATTERNS}
    base: dict[int, list] = {hz: [] for hz in horizons}
    seen = dict.fromkeys(PATTERNS, 0)
    stocks = 0
    for bars in frames.values():
        if len(bars) < SLOW + 20:
            continue
        stocks += 1
        found, ctx = detect(bars), context(bars, f["near"])
        mask = _mask(ctx, f).to_numpy()
        close = bars["Close"].astype(float).to_numpy()
        n = len(close)
        for key in PATTERNS:
            seen[key] += int((found[key].to_numpy() & mask).sum())
        for hz in horizons:
            if n <= hz:
                continue
            moves = np.full(n, np.nan)
            moves[: n - hz] = close[hz:] / close[: n - hz] - 1
            valid = ~np.isnan(moves) & mask
            base[hz].append(moves[valid])
            for key in PATTERNS:
                pat[key][hz].append(moves[valid & found[key].to_numpy()])
    join = lambda parts: np.concatenate(parts) if parts else np.array([])  # noqa: E731
    normal = {hz: _stats(join(base[hz])) for hz in horizons}
    rows = []
    for key in PATTERNS:
        row = {"key": key, "name": PATTERNS[key][0], "signal": PATTERNS[key][1], "seen": seen[key], "after": {}}
        for hz in horizons:
            s = _stats(join(pat[key][hz]))
            s["verdict"] = verdict(s["cases"], s["up_pct"], normal[hz]["up_pct"], min_cases)
            row["after"][str(hz)] = s
        rows.append(row)
    return {"patterns": rows, "normal": {str(k): v for k, v in normal.items()}, "filters": f, "stocks": stocks,
            "tested": len(PATTERNS) * len(horizons)}


# -- range of likely prices ahead ------------------------------------------------------------------------

QUANTILES = (0.1, 0.25, 0.5, 0.75, 0.9)
VOL_BARS = 20


def _quantile(sorted_values: np.ndarray, q: float) -> float:
    """Linear interpolation between closest ranks (numpy's default), written out so the browser matches."""
    pos = (len(sorted_values) - 1) * q
    lo = math.floor(pos)
    hi = min(lo + 1, len(sorted_values) - 1)
    return float(sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (pos - lo))


def cone(bars: pd.DataFrame, horizon: int = 10, *, closed: int | None = None) -> dict | None:
    """Where the price ended up 50% and 80% of the time over the next 1..``horizon`` candles, from this
    stock's own moves scaled to today's volatility. None when there is too little history."""
    close = bars["Close"].astype(float).to_numpy()[: len(bars) if closed is None else closed]
    n = len(close)
    if n < VOL_BARS + horizon + 60:
        return None
    logc = np.log(close)
    r1 = np.diff(logc)                                   # r1[k] = move from candle k to k+1
    sig = np.full(n, np.nan)                             # volatility known at candle i (20 moves ending at i)
    for i in range(VOL_BARS, n):
        sig[i] = np.std(r1[i - VOL_BARS:i], ddof=1)
    now = sig[n - 1]
    if not now > 0:
        return None

    def zs(h: int, upto: int) -> np.ndarray:
        """Volatility-scaled h-candle moves that started at or after the warm-up and ended before ``upto``."""
        idx = np.arange(VOL_BARS, upto - h)
        idx = idx[sig[idx] > 0]
        return np.sort((logc[idx + h] - logc[idx]) / (sig[idx] * math.sqrt(h)))

    steps = []
    for h in range(1, horizon + 1):
        z = zs(h, n)
        scale = now * math.sqrt(h)
        steps.append({"ahead": h, **{f"q{int(q * 100)}": float(close[-1] * math.exp(_quantile(z, q) * scale)) for q in QUANTILES}})
    # Out of sample: quantiles from the older two-thirds, tested on starts in the newest third.
    split = VOL_BARS + (n - VOL_BARS) * 2 // 3
    train = zs(horizon, split)
    starts = np.arange(split, n - horizon)
    starts = starts[sig[starts] > 0]
    held = None
    if len(train) >= 30 and len(starts) >= 20:
        lo, hi = _quantile(train, 0.1), _quantile(train, 0.9)
        z = (logc[starts + horizon] - logc[starts]) / (sig[starts] * math.sqrt(horizon))
        held = {"pct": _round(float(((z >= lo) & (z <= hi)).sum()) / len(starts) * 100, 1), "checks": int(len(starts))}
    return {"from": float(close[-1]), "steps": steps, "held": held, "samples": int(len(zs(horizon, n)))}
