"""Price charts for the learning notes, from real NSE end-of-day data.

Input: a folder of Yahoo Finance daily CSVs (Date, Open, High, Low, Close, Adj Close, Volume), one per symbol,
named like ``IDX_NSEI.csv`` for ^NSEI and ``RELIANCE.NS.csv`` for a stock, as the notes-data fetch writes them.
The examples are found by the rules in this file, not picked by eye from a chart, and the statistics quoted in
the notes are written to ``notes-stats.json`` next to the figures so every number can be traced.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from desk import indicators as ind
from desk.dev.notes_figures import (
    AQUA,
    BLUE,
    GREEN,
    INK,
    INK2,
    MUTED,
    ORANGE,
    OUT,
    RED,
    SURFACE,
    VIOLET,
    YELLOW,
)

SRC = "Source: NSE closing prices via Yahoo Finance end-of-day data"
STATS: dict = {}
START, END = "2015-01-01", None      # pattern scans and statistics cover 2015 to the last date in the data


# --- data ---------------------------------------------------------------------------------------------------------

def load(folder: Path, symbol: str) -> pd.DataFrame:
    name = symbol.replace("^", "IDX_") + ".csv"
    frame = pd.read_csv(folder / name, parse_dates=["Date"], index_col="Date")
    frame = frame[["Open", "High", "Low", "Close", "Volume"]].dropna()
    frame = frame[(frame["Close"] > 0) & (frame["High"] >= frame["Low"])]
    return frame[~frame.index.duplicated(keep="last")].sort_index()


def stock_symbols(folder: Path) -> list[str]:
    return sorted(p.name[:-4] for p in folder.glob("*.NS.csv"))


def _src(frame: pd.DataFrame) -> str:
    return f"{SRC}, {frame.index[0]:%d %b %Y} to {frame.index[-1]:%d %b %Y}."


# --- drawing ------------------------------------------------------------------------------------------------------

def candles(ax, bars: pd.DataFrame, width: float = 0.6) -> None:
    """Candlesticks on an integer x axis (no gaps for weekends); label dates with ``date_ticks``."""
    x = np.arange(len(bars))
    up = bars["Close"] >= bars["Open"]
    for color, mask in ((GREEN, up), (RED, ~up)):
        ax.vlines(x[mask], bars["Low"][mask], bars["High"][mask], color=color, linewidth=0.9)
        bottom = np.minimum(bars["Open"][mask], bars["Close"][mask])
        height = (bars["Close"][mask] - bars["Open"][mask]).abs()
        height = np.maximum(height, (bars["High"].max() - bars["Low"].min()) * 0.002)
        ax.bar(x[mask], height, width, bottom=bottom, color=color, edgecolor=color, linewidth=0.5)
    ax.set_xlim(-1, len(bars))


def date_ticks(ax, index: pd.DatetimeIndex, count: int = 6, fmt: str = "%d %b %y") -> None:
    step = max(len(index) // count, 1)
    ticks = list(range(0, len(index), step))
    ax.set_xticks(ticks)
    ax.set_xticklabels([index[i].strftime(fmt) for i in ticks])


def _thousands_y(ax) -> None:
    from matplotlib.ticker import FuncFormatter

    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:,.0f}"))


def _log_axis(ax, steps=(2000, 3000, 5000, 7500, 10000, 15000, 20000, 25000, 30000)) -> None:
    """Readable ticks on a log price axis."""
    from matplotlib.ticker import FixedLocator, FuncFormatter, NullFormatter, NullLocator

    ax.set_yscale("log")
    lo, hi = ax.get_ylim()
    ax.yaxis.set_major_locator(FixedLocator([t for t in steps if lo <= t <= hi]))
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:,.0f}"))
    ax.yaxis.set_minor_locator(NullLocator())
    ax.yaxis.set_minor_formatter(NullFormatter())


def _mark(ax, x, y, text, color=INK, dy=12, marker=True, dx=0) -> None:
    if marker:
        ax.plot([x], [y], "o", color=color, markersize=7, markeredgecolor=SURFACE, markeredgewidth=2, zorder=5)
    ax.annotate(text, (x, y), textcoords="offset points", xytext=(dx, dy), ha="center", fontsize=8.5, color=INK,
                zorder=6)


# --- swings -------------------------------------------------------------------------------------------------------

def zigzag(close: pd.Series, pct: float) -> list[tuple[int, float, str]]:
    """Swing highs and lows: a swing ends when price reverses ``pct`` percent from its extreme.

    Returns (position, price, 'H' or 'L'). Uses closes, so it never looks at a bar's future.
    """
    values = close.to_numpy()
    pivots, direction, ext_i, ext_v = [], 0, 0, values[0]
    for i, v in enumerate(values):
        if direction == 1 and v > ext_v or direction == -1 and v < ext_v:
            ext_i, ext_v = i, v
        if direction == 0:
            if v >= values[0] * (1 + pct / 100):
                direction, ext_i, ext_v = 1, i, v
            elif v <= values[0] * (1 - pct / 100):
                direction, ext_i, ext_v = -1, i, v
            continue
        if direction == 1 and v <= ext_v * (1 - pct / 100):
            pivots.append((ext_i, ext_v, "H"))
            direction, ext_i, ext_v = -1, i, v
        elif direction == -1 and v >= ext_v * (1 + pct / 100):
            pivots.append((ext_i, ext_v, "L"))
            direction, ext_i, ext_v = 1, i, v
    return pivots


def swing_labels(pivots):
    """HH/LH for highs and HL/LL for lows, each against the previous swing of its kind."""
    out, last = [], {"H": None, "L": None}
    for i, v, kind in pivots:
        prev = last[kind]
        if prev is None:
            label = "High" if kind == "H" else "Low"
        elif kind == "H":
            label = "HH" if v > prev else "LH"
        else:
            label = "HL" if v > prev else "LL"
        out.append((i, v, kind, label))
        last[kind] = v
    return out


# --- 1. Trends ----------------------------------------------------------------------------------------------------

def trend_figures(plt, folder: Path, save) -> None:
    nifty = load(folder, "^NSEI")
    window = nifty.loc["2024-06-01":]
    close = window["Close"]
    pivots = swing_labels(zigzag(close, 5))
    fig, ax = plt.subplots(figsize=(10, 4.2))
    ax.plot(range(len(close)), close.to_numpy(), color=BLUE, linewidth=1.6)
    xs = [p[0] for p in pivots]
    ax.plot(xs, [p[1] for p in pivots], color=MUTED, linewidth=1, linestyle="--")
    for i, v, kind, label in pivots:
        color = GREEN if label in ("HH", "HL") else RED if label in ("LH", "LL") else INK2
        near = [q[0] for q in pivots if q[2] == kind and q[0] != i and abs(q[0] - i) < 30]
        dx = 0 if not near else (-16 if near[0] > i else 16)
        _mark(ax, i, v, f"{label}\n{v:,.0f}", color=color, dy=8 if kind == "H" else -26, dx=dx)
    ax.set_title("Nifty 50: swing highs and swing lows (a swing ends on a 5% reversal)")
    date_ticks(ax, close.index, 8, "%b %y")
    _thousands_y(ax)
    ax.margins(y=0.12)
    save(fig, "trend-swings.png", _src(window) + " HH higher high, HL higher low, LH lower high, LL lower low.")
    STATS["trend_swings"] = [{"date": f"{close.index[i]:%Y-%m-%d}", "close": round(v, 1), "label": lab}
                             for i, v, _, lab in pivots]

    # Moving averages and crosses over a long stretch.
    long = nifty.loc["2014-01-01":].copy()
    long["SMA50"], long["SMA200"] = ind.sma(long["Close"], 50), ind.sma(long["Close"], 200)
    view = long.loc["2016-01-01":]
    above = (view["SMA50"] > view["SMA200"]).astype(int)
    cross = above.diff().fillna(0)
    fig, ax = plt.subplots(figsize=(10, 4.2))
    ax.plot(view.index, view["Close"], color=INK2, linewidth=1, label="Nifty 50 close")
    ax.plot(view.index, view["SMA50"], color=BLUE, linewidth=1.6, label="50-day average")
    ax.plot(view.index, view["SMA200"], color=ORANGE, linewidth=1.6, label="200-day average")
    golden, death = view.index[cross == 1], view.index[cross == -1]
    ax.plot(golden, view.loc[golden, "SMA200"], "^", color=GREEN, markersize=8, markeredgecolor=SURFACE,
            label="50 crosses above 200")
    ax.plot(death, view.loc[death, "SMA200"], "v", color=RED, markersize=8, markeredgecolor=SURFACE,
            label="50 crosses below 200")
    _log_axis(ax, (7500, 10000, 12500, 15000, 20000, 25000, 30000))
    ax.set_title("Nifty 50 with its 50-day and 200-day moving averages (log scale)")
    ax.legend(fontsize=8, loc="upper left")
    save(fig, "trend-ma.png", _src(view))
    STATS["ma_crosses"] = {"golden": [f"{d:%Y-%m-%d}" for d in golden], "death": [f"{d:%Y-%m-%d}" for d in death]}
    pct_above = float((view["Close"] > view["SMA200"]).mean() * 100)
    STATS["nifty_days_above_200dma_pct"] = round(pct_above, 1)

    # Support and resistance: the September 2024 high.
    sr = nifty.loc["2024-06-01":]
    level = float(nifty.loc["2024-09-01":"2024-10-31", "High"].max())
    day = nifty.loc["2024-09-01":"2024-10-31", "High"].idxmax()
    fig, ax = plt.subplots(figsize=(10, 4.2))
    candles(ax, sr.resample("W-FRI").agg({"Open": "first", "High": "max", "Low": "min", "Close": "last"}).dropna())
    weekly = sr.resample("W-FRI").agg({"Open": "first", "High": "max", "Low": "min", "Close": "last"}).dropna()
    ax.axhline(level, color=ORANGE, linewidth=1.4, linestyle="--")
    ax.text(1, level, f"  High of {day:%d %b %Y}: {level:,.0f}", color=INK, fontsize=8.5, va="bottom")
    low_level = float(nifty.loc["2025-03-01":"2025-04-30", "Low"].min())
    low_day = nifty.loc["2025-03-01":"2025-04-30", "Low"].idxmin()
    ax.axhline(low_level, color=AQUA, linewidth=1.4, linestyle="--")
    ax.text(1, low_level, f"  Low of {low_day:%d %b %Y}: {low_level:,.0f}", color=INK, fontsize=8.5, va="bottom")
    lows26 = nifty.loc["2026-01-01":, "Low"]
    first26 = lows26.loc[:"2026-06-30"].idxmin()
    second26 = lows26.loc["2026-07-01":].idxmin()
    zone = float(min(lows26[first26], lows26[second26]))
    ax.axhline(zone, color=VIOLET, linewidth=1.4, linestyle="--")
    ax.text(len(weekly) * 0.45, zone, f"  Lows of {first26:%d %b} and {second26:%d %b %Y}: "
            f"{lows26[first26]:,.0f} and {lows26[second26]:,.0f}", color=INK, fontsize=8.5, va="bottom")
    date_ticks(ax, weekly.index, 8, "%b %y")
    _thousands_y(ax)
    ax.set_title("Nifty 50 weekly candles: old highs and lows as resistance and support")
    save(fig, "sr-levels.png", _src(sr))
    touches = weekly[(weekly["High"] >= level * 0.99)]
    later = nifty.loc[day + pd.Timedelta(days=1):]
    above_high = later[later["High"] > level]
    above_close = later[later["Close"] > level]
    STATS["sr_break"] = {"first_high_above": f"{above_high.index[0]:%Y-%m-%d}" if len(above_high) else None,
                         "max_high": round(float(later["High"].max()), 1), "max_high_date": f"{later['High'].idxmax():%Y-%m-%d}",
                         "closes_above": len(above_close),
                         "close_dates": [f"{d:%Y-%m-%d}" for d in above_close.index],
                         "low_2026_a": [f"{first26:%Y-%m-%d}", round(float(lows26[first26]), 1)],
                         "low_2026_b": [f"{second26:%Y-%m-%d}", round(float(lows26[second26]), 1)]}
    STATS["sr"] = {"high_date": f"{day:%Y-%m-%d}", "high": round(level, 1), "low_date": f"{low_day:%Y-%m-%d}",
                   "low": round(low_level, 1), "weeks_within_1pct_of_high": [f"{d:%Y-%m-%d}" for d in touches.index]}


# --- 2. Candlesticks ----------------------------------------------------------------------------------------------

def candle_parts(bars: pd.DataFrame) -> pd.DataFrame:
    o, h, low, c = bars["Open"], bars["High"], bars["Low"], bars["Close"]
    rng = (h - low).replace(0, np.nan)
    body = (c - o).abs()
    return pd.DataFrame({"range": rng, "body": body, "upper": h - np.maximum(o, c), "lower": np.minimum(o, c) - low,
                         "green": c > o, "red": c < o, "fall10": c / c.shift(10) - 1}, index=bars.index)


def find_patterns(bars: pd.DataFrame) -> dict[str, pd.Series]:
    p = candle_parts(bars)
    o, c = bars["Open"], bars["Close"]
    big = p["range"] >= 0.8 * ind.atr(bars, 14)          # a meaningful candle, not a quiet day's speck
    down, up = p["fall10"] <= -0.03, p["fall10"] >= 0.03
    doji = p["body"] <= 0.1 * p["range"]
    hammer = big & down & (p["lower"] >= 2 * p["body"]) & (p["upper"] <= 0.1 * p["range"]) & (p["body"] >= 0.05 * p["range"])
    star = big & up & (p["upper"] >= 2 * p["body"]) & (p["lower"] <= 0.1 * p["range"]) & (p["body"] >= 0.05 * p["range"])
    po, pc = o.shift(1), c.shift(1)
    bull_engulf = down & p["red"].shift(1, fill_value=False) & p["green"] & (o <= pc) & (c >= po) & \
        (p["body"] > p["body"].shift(1)) & big
    bear_engulf = up & p["green"].shift(1, fill_value=False) & p["red"] & (o >= pc) & (c <= po) & \
        (p["body"] > p["body"].shift(1)) & big
    b1, b2 = p["body"].shift(2), p["body"].shift(1)
    morning = down.shift(2, fill_value=False) & p["red"].shift(2, fill_value=False) & \
        (b1 >= 0.6 * p["range"].shift(2)) & (b2 <= 0.3 * b1) & \
        (np.maximum(o.shift(1), c.shift(1)) <= c.shift(2)) & p["green"] & (c >= (o.shift(2) + c.shift(2)) / 2)
    evening = up.shift(2, fill_value=False) & p["green"].shift(2, fill_value=False) & \
        (b1 >= 0.6 * p["range"].shift(2)) & (b2 <= 0.3 * b1) & \
        (np.minimum(o.shift(1), c.shift(1)) >= c.shift(2)) & p["red"] & (c <= (o.shift(2) + c.shift(2)) / 2)
    return {"Doji": doji & (down | up), "Hammer": hammer, "Shooting star": star, "Bullish engulfing": bull_engulf,
            "Bearish engulfing": bear_engulf, "Morning star": morning, "Evening star": evening}


BULLISH = {"Hammer", "Bullish engulfing", "Morning star"}
BEARISH = {"Shooting star", "Bearish engulfing", "Evening star"}
EXAMPLE_STOCKS = ["RELIANCE.NS", "HDFCBANK.NS", "ICICIBANK.NS", "INFY.NS", "TCS.NS", "SBIN.NS", "ITC.NS", "LT.NS",
                  "BHARTIARTL.NS", "AXISBANK.NS", "KOTAKBANK.NS", "MARUTI.NS", "SUNPHARMA.NS", "TITAN.NS"]


def candle_figures(plt, folder: Path, save) -> None:
    nifty = load(folder, "^NSEI")
    recent = nifty.iloc[-30:]
    fig, ax = plt.subplots(figsize=(10, 4))
    candles(ax, recent)
    for kind, color in (("green", GREEN), ("red", RED)):
        rows = [i for i in range(len(recent)) if (recent["Close"].iloc[i] > recent["Open"].iloc[i]) == (kind == "green")]
        if not rows:
            continue
        i = max(rows, key=lambda r: abs(recent["Close"].iloc[r] - recent["Open"].iloc[r]))
        bar = recent.iloc[i]
        ax.annotate(f"{bar.name:%d %b %Y}\nOpen {bar.Open:,.0f}\nHigh {bar.High:,.0f}\nLow {bar.Low:,.0f}\n"
                    f"Close {bar.Close:,.0f}", (i, bar.High), textcoords="offset points", xytext=(0, 14),
                    ha="center", fontsize=8, color=INK,
                    bbox={"boxstyle": "round,pad=0.3", "fc": SURFACE, "ec": color, "lw": 1})
    date_ticks(ax, recent.index, 6, "%d %b")
    _thousands_y(ax)
    ax.margins(y=0.3)
    ax.set_title("Nifty 50 daily candles, last 30 sessions")
    save(fig, "candle-recent.png", _src(recent))

    # Statistics over the Nifty 50 stocks, and one real example of each pattern.
    rows, examples, base_all, base_down, base_up = [], {}, [], [], []
    for sym in stock_symbols(folder):
        bars = load(folder, sym).loc[START:]
        if len(bars) < 300:
            continue
        fwd = bars["Close"].shift(-10) / bars["Close"] - 1
        p = candle_parts(bars)
        base_all.append(fwd.dropna())
        base_down.append(fwd[p["fall10"] <= -0.03].dropna())
        base_up.append(fwd[p["fall10"] >= 0.03].dropna())
        for name, hits in find_patterns(bars).items():
            for day in bars.index[hits.fillna(False).to_numpy()]:
                f = fwd.get(day)
                if f == f and f is not None:
                    rows.append({"pattern": name, "symbol": sym, "date": day, "fwd": float(f)})
            if sym in EXAMPLE_STOCKS:
                recent_hits = bars.index[hits.fillna(False).to_numpy()]
                recent_hits = recent_hits[(recent_hits >= "2023-01-01") & (recent_hits <= bars.index[-15])]
                for day in recent_hits:
                    examples.setdefault(name, []).append((sym, day))
    table = pd.DataFrame(rows)
    every = pd.concat(base_all)
    after_fall, after_rise = pd.concat(base_down), pd.concat(base_up)
    summary = {}
    for name, group in table.groupby("pattern"):
        summary[name] = {"count": len(group), "up_after_10d_pct": round(float((group["fwd"] > 0).mean() * 100), 1),
                         "avg_10d_pct": round(float(group["fwd"].mean() * 100), 2),
                         "median_10d_pct": round(float(group["fwd"].median() * 100), 2)}
    STATS["candles"] = {
        "patterns": summary, "universe": f"{len(stock_symbols(folder))} current Nifty 50 stocks",
        "period": f"{START} to {load(folder, '^NSEI').index[-1]:%Y-%m-%d}",
        "baseline_all": {"count": len(every), "up_pct": round(float((every > 0).mean() * 100), 1),
                         "avg_pct": round(float(every.mean() * 100), 2)},
        "baseline_after_3pct_fall": {"count": len(after_fall), "up_pct": round(float((after_fall > 0).mean() * 100), 1),
                                     "avg_pct": round(float(after_fall.mean() * 100), 2)},
        "baseline_after_3pct_rise": {"count": len(after_rise), "up_pct": round(float((after_rise > 0).mean() * 100), 1),
                                     "avg_pct": round(float(after_rise.mean() * 100), 2)},
    }

    shown = ["Hammer", "Shooting star", "Bullish engulfing", "Bearish engulfing", "Morning star", "Doji"]
    fig, axes = plt.subplots(3, 2, figsize=(10, 10))
    STATS["candle_examples"] = {}
    used = set()
    for ax, name in zip(axes.flat, shown, strict=True):
        # The most recent one in a large, well-known stock not already shown, so the panels cover several stocks.
        ranked = sorted(examples[name], key=lambda e: e[1], reverse=True)
        sym, day = next((e for e in ranked if e[0] not in used), ranked[0])
        used.add(sym)
        bars = load(folder, sym)
        at = bars.index.get_loc(day)
        width = 3 if name == "Morning star" else 2 if "engulfing" in name else 1
        view = bars.iloc[at - 20: at + 11]
        candles(ax, view)
        first = 20 - width + 1
        lo, hi = view["Low"].iloc[first:21].min(), view["High"].iloc[first:21].max()
        pad = (view["High"].max() - view["Low"].min()) * 0.03
        ax.add_patch(__import__("matplotlib").patches.Rectangle((first - 0.5, lo - pad), width, hi - lo + 2 * pad,
                                                                 fill=False, ec=VIOLET, lw=1.5))
        after = bars["Close"].iloc[at + 10] / bars["Close"].iloc[at] - 1
        ax.set_title(f"{name}: {sym[:-3]}, {day:%d %b %Y}\nClose 10 sessions later: {after:+.1%}", fontsize=10)
        date_ticks(ax, view.index, 4, "%d %b")
        _thousands_y(ax)
        STATS["candle_examples"][name] = {"symbol": sym, "date": f"{day:%Y-%m-%d}", "after_10d_pct": round(after * 100, 2)}
    fig.tight_layout()
    save(fig, "candle-patterns.png", SRC + ". The pattern is boxed; each panel shows 20 sessions before and 10 after.")


# --- 3. Chart patterns --------------------------------------------------------------------------------------------

def find_chart_patterns(bars: pd.DataFrame, pct: float = 6.0) -> list[dict]:
    """Double tops and bottoms and head-and-shoulders tops on swing points, confirmed by a close through the neckline.

    A swing point is known once price has reversed ``pct`` percent from it, which happens before the neckline
    break, so the confirmation day uses only what was known that day.
    """
    close = bars["Close"]
    piv = zigzag(close, pct)
    found = []

    def confirm(after: int, level: float, below: bool, limit: int = 60):
        for j in range(after + 1, min(after + limit, len(close))):
            if (close.iloc[j] < level) if below else (close.iloc[j] > level):
                return j
        return None

    def outcome(rec: dict, j: int, below: bool, height: float, invalid: float) -> dict:
        entry = float(close.iloc[j])
        target = entry - height if below else entry + height
        path = close.iloc[j + 1: j + 61]
        hit_target = bool(((path <= target) if below else (path >= target)).any())
        hit_invalid = bool(((path >= invalid) if below else (path <= invalid)).any())
        first_target = path.index[((path <= target) if below else (path >= target)).to_numpy()]
        first_invalid = path.index[((path >= invalid) if below else (path <= invalid)).to_numpy()]
        target_first = hit_target and (not hit_invalid or first_target[0] < first_invalid[0])
        fwd20 = float(close.iloc[j + 20] / entry - 1) if j + 20 < len(close) else None
        rec.update({"confirm": j, "entry": entry, "target": target, "target_first": target_first,
                    "failed_first": hit_invalid and not target_first, "fwd20": fwd20, "complete": j + 60 < len(close)})
        return rec

    for k in range(len(piv) - 2):
        (i1, v1, t1), (i2, v2, _), (i3, v3, _) = piv[k], piv[k + 1], piv[k + 2]
        if not 15 <= i3 - i1 <= 120 or abs(v3 / v1 - 1) > 0.02:
            continue
        if t1 == "H" and v2 <= min(v1, v3) * 0.92:
            j = confirm(i3, v2, below=True)
            if j is not None:
                found.append(outcome({"kind": "Double top", "points": [i1, i2, i3], "neck": v2}, j, True,
                                     max(v1, v3) - v2, max(v1, v3)))
        if t1 == "L" and v2 >= max(v1, v3) * 1.08:
            j = confirm(i3, v2, below=False)
            if j is not None:
                found.append(outcome({"kind": "Double bottom", "points": [i1, i2, i3], "neck": v2}, j, False,
                                     v2 - min(v1, v3), min(v1, v3)))
    for k in range(len(piv) - 4):
        (i1, h1, t1), (i2, l1, _), (i3, h2, _), (i4, l2, _), (i5, h3, _) = piv[k:k + 5]
        if t1 != "H" or not 20 <= i5 - i1 <= 160:
            continue
        if h2 >= max(h1, h3) * 1.03 and abs(h1 / h3 - 1) <= 0.04 and abs(l1 / l2 - 1) <= 0.04:
            neck = min(l1, l2)
            j = confirm(i5, neck, below=True)
            if j is not None:
                found.append(outcome({"kind": "Head and shoulders", "points": [i1, i2, i3, i4, i5], "neck": neck},
                                     j, True, h2 - neck, h2))
    return found


def chart_pattern_figures(plt, folder: Path, save) -> None:
    every, examples = [], {}
    for sym in stock_symbols(folder):
        bars = load(folder, sym).loc[START:]
        if len(bars) < 300:
            continue
        for rec in find_chart_patterns(bars):
            rec.update({"symbol": sym, "date": bars.index[rec["confirm"]]})
            every.append(rec)
            if sym in EXAMPLE_STOCKS and rec["complete"]:
                examples.setdefault(rec["kind"], []).append(rec)
    summary = {}
    for kind in ("Double top", "Double bottom", "Head and shoulders"):
        done = [r for r in every if r["kind"] == kind and r["complete"]]
        fwd = [r["fwd20"] for r in done if r["fwd20"] is not None]
        bearish = kind != "Double bottom"
        summary[kind] = {
            "count": len(done),
            "target_before_invalidation_pct": round(100 * sum(r["target_first"] for r in done) / len(done), 1) if done else None,
            "invalidated_first_pct": round(100 * sum(r["failed_first"] for r in done) / len(done), 1) if done else None,
            "moved_pattern_way_after_20d_pct": round(100 * sum((f < 0) if bearish else (f > 0) for f in fwd) / len(fwd), 1) if fwd else None,
            "avg_20d_pct": round(100 * float(np.mean(fwd)), 2) if fwd else None,
        }
    STATS["chart_patterns"] = summary

    fig, axes = plt.subplots(3, 1, figsize=(10, 12))
    STATS["chart_pattern_examples"] = {}
    used = set()
    for ax, kind in zip(axes, ("Double top", "Double bottom", "Head and shoulders"), strict=True):
        ranked = sorted(examples.get(kind, []), key=lambda r: r["date"], reverse=True)
        rec = next((r for r in ranked if r["symbol"] not in used), ranked[0])
        used.add(rec["symbol"])
        bars = load(folder, rec["symbol"]).loc[START:]
        first, last = max(rec["points"][0] - 25, 0), min(rec["confirm"] + 45, len(bars) - 1)
        view = bars.iloc[first:last + 1]
        candles(ax, view)
        pts = [p - first for p in rec["points"]]
        ax.plot(pts, [bars["Close"].iloc[p] for p in rec["points"]], color=VIOLET, linewidth=1.5, marker="o",
                markersize=6, markeredgecolor=SURFACE)
        ax.axhline(rec["neck"], color=ORANGE, linestyle="--", linewidth=1.3)
        ax.text(0.5, rec["neck"], "  Neckline", color=INK, fontsize=8.5, va="bottom")
        c = rec["confirm"] - first
        _mark(ax, c, rec["entry"], "Close through\nneckline", color=INK2, dy=-34 if kind != "Double bottom" else 14)
        ax.axhline(rec["target"], color=AQUA, linestyle=":", linewidth=1.3)
        ax.text(0.5, rec["target"], "  Measured target", color=INK, fontsize=8.5, va="bottom")
        verdict = "target reached first" if rec["target_first"] else \
            "pattern failed first" if rec["failed_first"] else "neither within 60 sessions"
        ax.set_title(f"{kind}: {rec['symbol'][:-3]}, confirmed {rec['date']:%d %b %Y} ({verdict})", fontsize=10)
        date_ticks(ax, view.index, 6, "%d %b %y")
        _thousands_y(ax)
        STATS["chart_pattern_examples"][kind] = {"symbol": rec["symbol"], "confirmed": f"{rec['date']:%Y-%m-%d}",
                                                 "neck": round(rec["neck"], 1), "target": round(rec["target"], 1),
                                                 "result": verdict}
    fig.tight_layout()
    save(fig, "chart-patterns.png", SRC + ". Swing points on closes, 6% reversal. Found by rule, not drawn by eye.")

    # A range (rectangle) breakout on volume.
    hits = []
    for sym in stock_symbols(folder):
        bars = load(folder, sym).loc[START:]
        if len(bars) < 300 or bars["Volume"].iloc[-250:].eq(0).mean() > 0.2:
            continue
        hi, lo = bars["High"].rolling(40).max().shift(1), bars["Low"].rolling(40).min().shift(1)
        tight = (hi / lo - 1) <= 0.10
        vol = bars["Volume"] >= 1.5 * bars["Volume"].rolling(50).mean().shift(1)
        signal = tight & (bars["Close"] > hi) & vol
        fwd = bars["Close"].shift(-20) / bars["Close"] - 1
        for day in bars.index[signal.fillna(False).to_numpy()]:
            if fwd.get(day) == fwd.get(day):
                hits.append({"symbol": sym, "date": day, "fwd20": float(fwd[day])})
    fwd = np.array([h["fwd20"] for h in hits])
    STATS["range_breakout"] = {"count": len(hits), "up_after_20d_pct": round(float((fwd > 0).mean() * 100), 1),
                               "avg_20d_pct": round(float(fwd.mean() * 100), 2),
                               "median_20d_pct": round(float(np.median(fwd) * 100), 2)}
    pick = max((h for h in hits if h["symbol"] in EXAMPLE_STOCKS), key=lambda h: h["date"])
    bars = load(folder, pick["symbol"]).loc[START:]
    at = bars.index.get_loc(pick["date"])
    view = bars.iloc[at - 60: at + 25]
    fig, (ax, axv) = plt.subplots(2, 1, figsize=(10, 5.6), sharex=True, gridspec_kw={"height_ratios": [3, 1]})
    candles(ax, view)
    hi, lo = float(bars["High"].iloc[at - 40:at].max()), float(bars["Low"].iloc[at - 40:at].min())
    ax.add_patch(__import__("matplotlib").patches.Rectangle((20 - 0.5, lo), 40, hi - lo, fill=True, fc=BLUE,
                                                             alpha=0.07, ec=BLUE, lw=1))
    _mark(ax, 60, float(bars["Close"].iloc[at]), "Close above\n40-day range", color=INK2, dy=10, dx=-60)
    colors = [GREEN if c >= o else RED for o, c in zip(view["Open"], view["Close"], strict=True)]
    axv.bar(range(len(view)), view["Volume"] / 1e5, color=colors, width=0.6)
    axv.plot(range(len(view)), bars["Volume"].rolling(50).mean().iloc[at - 60: at + 25].to_numpy() / 1e5,
             color=INK2, linewidth=1.2, label="50-day average")
    axv.set_ylabel("Volume (lakh)")
    axv.legend(fontsize=8, loc="upper left")
    after = bars["Close"].iloc[at + 20] / bars["Close"].iloc[at] - 1
    ax.set_title(f"Range breakout on volume: {pick['symbol'][:-3]}, {pick['date']:%d %b %Y}. "
                 f"Close 20 sessions later: {after:+.1%}", fontsize=10)
    date_ticks(axv, view.index, 6, "%d %b %y")
    _thousands_y(ax)
    fig.tight_layout()
    save(fig, "range-breakout.png", _src(view))
    STATS["range_breakout_example"] = {"symbol": pick["symbol"], "date": f"{pick['date']:%Y-%m-%d}",
                                       "after_20d_pct": round(float(after) * 100, 2)}


# --- 4. Indicators ------------------------------------------------------------------------------------------------

def indicator_figures(plt, folder: Path, save) -> None:
    nifty = load(folder, "^NSEI")
    full = nifty.loc["2023-01-01":].copy()
    view_from = nifty.index[-260]
    view = full.loc[view_from:]
    x = np.arange(len(view))

    def price(ax, title):
        ax.plot(x, view["Close"], color=INK2, linewidth=1.3, label="Nifty 50 close")
        ax.set_title(title)
        _thousands_y(ax)

    def panel(name, rows, title, fill=None, sub_label=None):
        fig, axes = plt.subplots(2, 1, figsize=(10, 5.6), sharex=True, gridspec_kw={"height_ratios": [2.2, 1]})
        price(axes[0], title)
        rows(axes)
        if sub_label:
            axes[1].set_ylabel(sub_label)
        date_ticks(axes[1], view.index, 7, "%b %y")
        axes[0].legend(fontsize=8, loc="upper left")
        fig.tight_layout()
        save(fig, name, _src(view))

    r = ind.rsi(full["Close"], 14).loc[view_from:]

    def rsi_rows(axes):
        axes[1].plot(x, r, color=VIOLET, linewidth=1.4)
        for level in (70, 30):
            axes[1].axhline(level, color=MUTED, linestyle="--", linewidth=1)
        axes[1].set_ylim(10, 90)
        for i in np.where((r < 30) & (r.shift(1) >= 30))[0]:
            axes[0].plot([i], [view["Close"].iloc[i]], "v", color=RED, markersize=7, markeredgecolor=SURFACE)
        for i in np.where((r > 70) & (r.shift(1) <= 70))[0]:
            axes[0].plot([i], [view["Close"].iloc[i]], "^", color=GREEN, markersize=7, markeredgecolor=SURFACE)
    panel("ind-rsi.png", rsi_rows, "RSI (14): markers where RSI enters above 70 (▲) or below 30 (▼)", sub_label="RSI")

    line, sig, hist = (s.loc[view_from:] for s in ind.macd(full["Close"]))

    def macd_rows(axes):
        axes[1].bar(x, hist, color=[GREEN if h >= 0 else RED for h in hist], width=0.8, alpha=0.6)
        axes[1].plot(x, line, color=BLUE, linewidth=1.4, label="MACD line")
        axes[1].plot(x, sig, color=ORANGE, linewidth=1.4, label="Signal line")
        axes[1].axhline(0, color=MUTED, linewidth=1)
        axes[1].legend(fontsize=8, loc="lower left", ncols=2)
    panel("ind-macd.png", macd_rows, "MACD (12, 26, 9)", sub_label="Points")

    upper, mid, lower = (s.loc[view_from:] for s in ind.bollinger(full["Close"], 20, 2))
    st_line, st_trend = (s.loc[view_from:] for s in ind.supertrend(full, 10, 3))
    fig, axes = plt.subplots(2, 1, figsize=(10, 7.4), sharex=True)
    price(axes[0], "Bollinger Bands (20-day average ± 2 standard deviations)")
    axes[0].fill_between(x, lower, upper, color=BLUE, alpha=0.10, linewidth=0)
    axes[0].plot(x, upper, color=BLUE, linewidth=1)
    axes[0].plot(x, lower, color=BLUE, linewidth=1)
    axes[0].plot(x, mid, color=BLUE, linewidth=1, linestyle="--", label="20-day average")
    axes[0].legend(fontsize=8, loc="upper left")
    price(axes[1], "Supertrend (10, 3): green while price is above the band, red while below")
    long_line = st_line.where(st_trend == 1)
    short_line = st_line.where(st_trend == 0)
    axes[1].plot(x, long_line, color=GREEN, linewidth=1.6)
    axes[1].plot(x, short_line, color=RED, linewidth=1.6)
    date_ticks(axes[1], view.index, 7, "%b %y")
    fig.tight_layout()
    save(fig, "ind-bollinger-supertrend.png", _src(view))

    a = ind.atr(full, 14).loc[view_from:]
    adx, plus_di, minus_di = (s.loc[view_from:] for s in ind.adx(full, 14))
    fig, axes = plt.subplots(3, 1, figsize=(10, 7.6), sharex=True, gridspec_kw={"height_ratios": [2, 1, 1]})
    price(axes[0], "ATR and ADX")
    axes[1].plot(x, a, color=ORANGE, linewidth=1.4)
    axes[1].set_ylabel("ATR (14), points")
    axes[2].plot(x, adx, color=INK, linewidth=1.6, label="ADX")
    axes[2].plot(x, plus_di, color=GREEN, linewidth=1.1, label="+DI")
    axes[2].plot(x, minus_di, color=RED, linewidth=1.1, label="−DI")
    axes[2].axhline(25, color=MUTED, linestyle="--", linewidth=1)
    axes[2].legend(fontsize=8, loc="upper left", ncols=3)
    date_ticks(axes[2], view.index, 7, "%b %y")
    fig.tight_layout()
    save(fig, "ind-atr-adx.png", _src(view))
    STATS["indicators_latest"] = {"date": f"{view.index[-1]:%Y-%m-%d}", "close": round(float(view['Close'].iloc[-1]), 1),
                                  "rsi14": round(float(r.iloc[-1]), 1), "atr14": round(float(a.iloc[-1]), 1),
                                  "adx14": round(float(adx.iloc[-1]), 1)}
    rsi_all = ind.rsi(nifty["Close"], 14)
    STATS["rsi_extremes_since_2008"] = {"days_below_30_pct": round(float((rsi_all < 30).mean() * 100), 1),
                                        "days_above_70_pct": round(float((rsi_all > 70).mean() * 100), 1)}


# --- 5. Volatility and risk ---------------------------------------------------------------------------------------

def volatility_figures(plt, folder: Path, save) -> None:
    nifty = load(folder, "^NSEI")
    vix = load(folder, "^INDIAVIX")["Close"]
    start = max(nifty.index[0], vix.index[0])
    close = nifty["Close"].loc[start:]
    vix = vix.loc[start:]
    vix = vix[(vix > 5) & (vix < 100)]
    fig, axes = plt.subplots(2, 1, figsize=(10, 6), sharex=True)
    axes[0].plot(close.index, close, color=BLUE, linewidth=1.2)
    _log_axis(axes[0])
    axes[0].set_title("Nifty 50 (log scale)")
    axes[1].plot(vix.index, vix, color=ORANGE, linewidth=1)
    axes[1].set_title("India VIX: the market's expected volatility for the next 30 days, % a year")
    for day in (vix.idxmax(),):
        _mark(axes[1], day, vix[day], f"{vix[day]:.1f} on {day:%d %b %Y}", color=ORANGE, dy=-4, marker=True)
        axes[1].texts[-1].set_ha("left")
        axes[1].texts[-1].set_position((8, -4))
    fig.tight_layout()
    save(fig, "vol-vix.png", f"{SRC}; India VIX from NSE via Yahoo Finance, {start:%b %Y} to {close.index[-1]:%b %Y}.")

    ret = np.log(close).diff()
    realized = ret.rolling(21).std() * np.sqrt(252) * 100
    both = pd.concat({"realized": realized.shift(-21), "vix": vix}, axis=1).dropna().loc["2015-01-01":]
    fig, ax = plt.subplots(figsize=(10, 3.8))
    ax.plot(both.index, both["vix"], color=ORANGE, linewidth=1.1, label="India VIX on the day (expected)")
    ax.plot(both.index, both["realized"], color=BLUE, linewidth=1.1, label="Nifty's actual volatility over the next 21 sessions")
    ax.set_ylabel("% a year")
    ax.legend(fontsize=8, loc="upper left")
    ax.set_title("Expected against actual volatility")
    save(fig, "vol-implied-realized.png", _src(nifty.loc["2015-01-01":]) + " Actual = annualised std. dev. of daily log returns.")
    STATS["vix"] = {"max": round(float(vix.max()), 2), "max_date": f"{vix.idxmax():%Y-%m-%d}",
                    "latest": round(float(vix.iloc[-1]), 2), "latest_date": f"{vix.index[-1]:%Y-%m-%d}",
                    "median_since_2015": round(float(vix.loc['2015-01-01':].median()), 2),
                    "vix_above_realized_pct_since_2015": round(float((both['vix'] > both['realized']).mean() * 100), 1)}

    daily = close.pct_change().dropna() * 100
    fig, ax = plt.subplots(figsize=(10, 3.6))
    ax.hist(daily.clip(-6, 6), bins=120, color=BLUE)
    ax.set_title("Nifty 50 daily moves")
    ax.set_xlabel("Change from previous close, % (moves beyond ±6% shown at the edge)")
    ax.set_ylabel("Number of days")
    save(fig, "vol-daily-moves.png", _src(nifty.loc[start:]))
    STATS["daily_moves"] = {"days": len(daily), "abs_over_2pct": int((daily.abs() > 2).sum()),
                            "abs_over_3pct": int((daily.abs() > 3).sum()), "abs_over_5pct": int((daily.abs() > 5).sum()),
                            "worst": round(float(daily.min()), 2), "worst_date": f"{daily.idxmin():%Y-%m-%d}",
                            "best": round(float(daily.max()), 2), "best_date": f"{daily.idxmax():%Y-%m-%d}",
                            "since": f"{daily.index[0]:%Y-%m-%d}",
                            "abs_over_2pct_since_2015": int((daily.loc['2015-01-01':].abs() > 2).sum()),
                            "days_since_2015": int(len(daily.loc['2015-01-01':]))}

    all_close = nifty["Close"]
    dd = (all_close / all_close.cummax() - 1) * 100
    fig, ax = plt.subplots(figsize=(10, 3.6))
    ax.fill_between(dd.index, dd, 0, color=RED, alpha=0.25, linewidth=0)
    ax.plot(dd.index, dd, color=RED, linewidth=1)
    ax.set_title("Nifty 50: fall from its previous peak (drawdown)")
    ax.set_ylabel("%")
    episodes = []
    in_dd, peak_day = False, all_close.index[0]
    for day, value in dd.items():
        if value == 0:
            peak_day = day
        if value <= -20 and not in_dd:
            in_dd = True
        if in_dd and value == 0:
            in_dd = False
        episodes.append(peak_day)
    worst = []
    for peak in sorted(set(episodes)):
        seg = dd.loc[peak:]
        nxt = seg[(seg == 0) & (seg.index > peak)]
        seg = seg.loc[:nxt.index[0]] if len(nxt) else seg
        if seg.min() <= -20:
            worst.append({"peak": f"{peak:%Y-%m-%d}", "trough": f"{seg.idxmin():%Y-%m-%d}", "fall_pct": round(float(seg.min()), 1),
                          "recovered": f"{nxt.index[0]:%Y-%m-%d}" if len(nxt) else None})
            _mark(ax, seg.idxmin(), seg.min(), f"{seg.min():.0f}%", color=RED, dy=-14, marker=False)
    save(fig, "risk-drawdowns.png", _src(nifty))
    STATS["nifty_drawdowns_over_20pct"] = worst


# --- 6. Strategy backtests ----------------------------------------------------------------------------------------

TESTS = [("buy_hold", {}, "Buy and hold"), ("sma_cross", {"fast": 50, "slow": 200}, "50/200-day average cross"),
         ("donchian", {"entry": 20, "exit": 10}, "20-day high breakout, 10-day low exit"),
         ("supertrend", {"period": 10, "multiplier": 3}, "Supertrend (10, 3)"),
         ("macd", {}, "MACD above signal (12, 26, 9)"),
         ("rsi_reversion", {"period": 14, "buy_below": 30, "sell_above": 60}, "RSI below 30, exit above 60")]


def backtest_figures(plt, folder: Path, save) -> None:
    from desk.backtest import BacktestParams, run_strategy

    nifty = load(folder, "^NSEI").loc["2010-01-01":]
    rows, curves = [], {}
    for key, params, label in TESTS:
        out = run_strategy(nifty, key, params, BacktestParams(capital=1_000_000))
        m = out["metrics"]
        rows.append({"strategy": label, "cagr_pct": m["cagr_pct"], "max_drawdown_pct": m["max_drawdown_pct"],
                     "trades": m["trades"], "win_rate_pct": m["win_rate_pct"], "exposure_pct": m["exposure_pct"],
                     "charges": m["total_charges"], "final_value": m["final_value"]})
        curves[label] = pd.Series({pd.Timestamp(d): v for d, v in out["equity"]})
    STATS["backtest_nifty"] = {"period": f"{nifty.index[0]:%Y-%m-%d} to {nifty.index[-1]:%Y-%m-%d}", "rows": rows}
    fig, ax = plt.subplots(figsize=(10, 4.2))
    for (label, curve), color in zip(curves.items(), (INK2, BLUE, ORANGE, AQUA, YELLOW, VIOLET), strict=True):
        ax.plot(curve.index, curve / 1e5, color=color, linewidth=1.8 if label == "Buy and hold" else 1.3, label=label)
    _log_axis(ax, (5, 7.5, 10, 15, 20, 30, 50, 75))
    ax.yaxis.set_major_formatter(__import__("matplotlib").ticker.FuncFormatter(lambda v, _: f"₹{v:g} lakh"))
    ax.set_title("₹10 lakh in each strategy on the Nifty 50, after charges (log scale)")
    ax.legend(fontsize=8, loc="upper left")
    save(fig, "bt-nifty.png", _src(nifty) + " Desk backtester: delivery charges, 0.05% slippage, no dividends.")

    # Same rules on each current Nifty 50 stock, 2015 onward.
    per = {label: [] for _, _, label in TESTS}
    for sym in stock_symbols(folder):
        bars = load(folder, sym).loc[START:]
        if len(bars) < 750:
            continue
        hold = None
        for key, params, label in TESTS:
            try:
                m = run_strategy(bars, key, params, BacktestParams(capital=1_000_000))["metrics"]
            except ValueError:
                continue
            if key == "buy_hold":
                hold = m["cagr_pct"]
            per[label].append({"symbol": sym, "cagr": m["cagr_pct"], "dd": m["max_drawdown_pct"], "hold": hold})
    summary = {}
    for label, items in per.items():
        if not items:
            continue
        summary[label] = {"stocks": len(items),
                          "median_cagr_pct": round(float(np.median([i["cagr"] for i in items])), 2),
                          "median_max_drawdown_pct": round(float(np.median([i["dd"] for i in items])), 2),
                          "beat_buy_hold_pct": round(100 * float(np.mean([i["cagr"] > i["hold"] for i in items])), 1),
                          "lost_money_pct": round(100 * float(np.mean([i["cagr"] < 0 for i in items])), 1)}
    STATS["backtest_stocks"] = {"period": f"{START} to last date", "universe": "current Nifty 50 stocks with 3+ years",
                                "rows": summary}


def build(plt, folder: Path, save) -> None:
    folder = Path(folder)
    trend_figures(plt, folder, save)
    candle_figures(plt, folder, save)
    chart_pattern_figures(plt, folder, save)
    indicator_figures(plt, folder, save)
    volatility_figures(plt, folder, save)
    backtest_figures(plt, folder, save)
    nifty = load(folder, "^NSEI")
    lots = pd.read_csv(folder / "fo_mktlots.csv", skipinitialspace=True)
    lots.columns = [c.strip() for c in lots.columns]
    month = lots.columns[2]
    STATS["lots"] = {"month": month, **{str(r["SYMBOL"]).strip(): int(str(r[month]).strip())
                                        for _, r in lots.iterrows() if str(r[month]).strip().isdigit()}}
    STATS["closes"] = {s: round(float(load(folder, s)["Close"].iloc[-1]), 2) for s in ("^NSEI", "^NSEBANK")}
    STATS["data"] = {"first": f"{nifty.index[0]:%Y-%m-%d}", "last": f"{nifty.index[-1]:%Y-%m-%d}",
                     "nifty_last_close": round(float(nifty['Close'].iloc[-1]), 2)}
    (OUT.parent / "notes-stats.json").write_text(json.dumps(STATS, indent=1, default=str) + "\n", encoding="utf-8")
