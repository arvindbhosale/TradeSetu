"""Chart data: candles at any interval, with indicator overlays and lower panels."""

from __future__ import annotations

import calendar

import pandas as pd

from . import indicators as ind

OVERLAYS = {
    "sma20": "SMA 20", "sma50": "SMA 50", "sma200": "SMA 200", "ema20": "EMA 20",
    "vwap": "VWAP (intraday)", "bollinger": "Bollinger Bands (20, 2)", "supertrend": "Supertrend (10, 3)",
    "psar": "Parabolic SAR", "donchian": "Donchian channel (20)",
    "pivots_d": "Pivots, daily (traditional)", "pivots_w": "Pivots, weekly (traditional)",
    "pivots_m": "Pivots, monthly (traditional)",
}
# The pivot overlays by timeframe. ``pivots``, the one overlay older clients send, is daily on intraday charts
# and monthly on day charts.
PIVOT_OVERLAYS = {"pivots_d": "D", "pivots_w": "W", "pivots_m": "M"}
PANELS = {"volume": "Volume", "rsi": "RSI (14)", "macd": "MACD (12, 26, 9)", "adx": "ADX (14)",
          "stochastic": "Stochastic (14, 3)"}
MAX_BARS = 1500

# Kept under its old name for callers that import it from here.
pivot_levels = ind.pivot_levels


def _time(stamp: pd.Timestamp, intraday: bool):
    """The chart library's time: a date for daily bars, else seconds with IST wall time kept as-is."""
    if not intraday:
        return stamp.strftime("%Y-%m-%d")
    return calendar.timegm(stamp.timetuple())


def _points(series: pd.Series, window: pd.Index, intraday: bool) -> list:
    return [{"time": _time(stamp, intraday), "value": round(float(value), 2)}
            for stamp, value in series.reindex(window).items() if pd.notna(value)]


def build(bars: pd.DataFrame, interval: str, overlays: list[str], panels: list[str],
          start: str | None = None) -> dict:
    """Candles with the chosen overlays and panels; indicators are warmed up on all of ``bars``."""
    intraday = interval != "1d"
    overlays = [("pivots_d" if intraday else "pivots_m") if o == "pivots" else o for o in overlays]
    unknown = [o for o in overlays if o not in OVERLAYS]
    if unknown:
        raise ValueError(f"unknown overlay: {', '.join(unknown)}")
    unknown = [p for p in panels if p not in PANELS]
    if unknown:
        raise ValueError(f"unknown panel: {', '.join(unknown)}")
    if "vwap" in overlays and not intraday:
        raise ValueError("VWAP restarts each day, so it needs an intraday interval.")
    overlays, panels = list(dict.fromkeys(overlays)), list(dict.fromkeys(panels))
    window = bars.index if start is None else bars.index[bars.index >= pd.Timestamp(start)]
    if len(window) == 0:
        raise ValueError("No prices in that date range.")
    truncated = len(window) > MAX_BARS
    window = window[-MAX_BARS:]
    close, view = bars["Close"], bars.reindex(window)

    def line(key, name, series, **style):
        return {"key": key, "name": name, "points": _points(series, window, intraday), **style}

    lines, pivot_tables = [], []
    for key in overlays:
        if key.startswith("sma"):
            lines.append(line(key, OVERLAYS[key], ind.sma(close, int(key[3:]))))
        elif key == "ema20":
            lines.append(line(key, OVERLAYS[key], ind.ema(close, 20)))
        elif key == "vwap":
            lines.append(line(key, "VWAP", ind.vwap(bars)))
        elif key == "bollinger":
            upper, mid, lower = ind.bollinger(close, 20, 2.0)
            lines += [line(key, "Bollinger upper", upper), line(key, "Bollinger mid", mid, thin=True),
                      line(key, "Bollinger lower", lower)]
        elif key == "supertrend":
            lines.append(line(key, OVERLAYS[key], ind.supertrend(bars, 10, 3.0)[0], step=True))
        elif key == "psar":
            lines.append(line(key, OVERLAYS[key], ind.parabolic_sar(bars)[0], dots=True))
        elif key == "donchian":
            high, low = ind.donchian(bars, 20)
            lines += [line(key, "Donchian high", high, step=True), line(key, "Donchian low", low, step=True)]
        elif key in PIVOT_OVERLAYS:
            freq = PIVOT_OVERLAYS[key]
            levels = ind.period_pivots(bars, freq)
            word = ind.PIVOT_PERIODS[freq]
            for name in ind.PIVOT_LEVELS:
                lines.append(line(key, f"Pivot {name} ({word})", levels[name], step=True, dashed=name != "P"))
            pivot_tables.append(_pivot_table(bars, levels, freq, intraday))

    lower = []
    for panel in panels:
        if panel == "volume":
            up = (view["Close"] >= view["Open"]).to_numpy()
            lower.append({"key": panel, "name": PANELS[panel], "kind": "bars", "points": [
                {"time": _time(stamp, intraday), "value": int(volume), "up": bool(flag)}
                for (stamp, volume), flag in zip(view["Volume"].fillna(0).items(), up, strict=True)]})
        elif panel == "rsi":
            lower.append({"key": panel, "name": PANELS[panel], "kind": "line", "guides": [30, 70],
                          "lines": [line(panel, "RSI", ind.rsi(close, 14))]})
        elif panel == "macd":
            macd, signal, hist = ind.macd(close)
            lower.append({"key": panel, "name": PANELS[panel], "kind": "line", "guides": [0],
                          "lines": [line(panel, "MACD", macd), line(panel, "Signal", signal)],
                          "histogram": _points(hist, window, intraday)})
        elif panel == "adx":
            adx, plus_di, minus_di = ind.adx(bars, 14)
            lower.append({"key": panel, "name": PANELS[panel], "kind": "line", "guides": [25],
                          "lines": [line(panel, "ADX", adx), line(panel, "+DI", plus_di), line(panel, "−DI", minus_di)]})
        elif panel == "stochastic":
            k, d = ind.stochastic(bars, 14, 3)
            lower.append({"key": panel, "name": PANELS[panel], "kind": "line", "guides": [20, 80],
                          "lines": [line(panel, "%K", k), line(panel, "%D", d)]})

    return {
        "interval": interval, "intraday": intraday,
        "candles": [{"time": _time(stamp, intraday), "open": round(float(r.Open), 2), "high": round(float(r.High), 2),
                     "low": round(float(r.Low), 2), "close": round(float(r.Close), 2)}
                    for stamp, r in zip(window, view.itertuples(), strict=True)],
        "lines": lines, "panels": lower, "truncated": truncated,
        "first": window[0].strftime("%Y-%m-%d %H:%M" if intraday else "%Y-%m-%d"),
        "last": window[-1].strftime("%Y-%m-%d %H:%M" if intraday else "%Y-%m-%d"),
        "pivots": [t for t in pivot_tables if t] or None,
    }


def _pivot_table(bars: pd.DataFrame, levels: pd.DataFrame, freq: str, intraday: bool) -> dict | None:
    """The levels a trader works with now: on a day chart, daily ones for the next session (from the last
    bar); otherwise those in force for the newest bar's day, week or month, set by the period before."""
    word = ind.PIVOT_PERIODS[freq]
    if freq == "D" and not intraday:
        last = bars.iloc[-1]
        values = ind.pivot_levels(float(last["High"]), float(last["Low"]), float(last["Close"]))
        when = f"the session after {bars.index[-1].strftime('%Y-%m-%d')}"
    else:
        row = levels.iloc[-1]
        if row.isna().any():
            return None
        values = row.to_dict()
        stamp = bars.index[-1]
        when = {"D": f"{stamp.strftime('%Y-%m-%d')}, from the previous session",
                "W": f"the week of {(stamp - pd.Timedelta(days=stamp.weekday())).strftime('%Y-%m-%d')}, from the week before",
                "M": f"{stamp.strftime('%B %Y')}, from the month before"}[freq]
    return {"key": f"pivots_{freq.lower()}", "timeframe": word, "for": when,
            "levels": {name: round(float(values[name]), 2) for name in ind.PIVOT_LEVELS}}
