"""Candles for the Charts tab from one interface: crypto exchanges' public data, or the connected broker.

Every provider answers ``candles(symbol, timeframe)`` with bars in ascending time, or raises
``MarketDataError`` saying which provider failed and why. Nothing here falls back to Yahoo or scrapes an
exchange website: crypto comes from Binance's public market-data API (Coinbase Exchange when Binance does not
answer), and NSE/BSE come from the broker the user connected with their own key.

Bar times are seconds since 1970 on the India clock (UTC + 5:30), the way the rest of the desk draws charts,
so a chart reads in IST without the browser converting. ``SESSION_OFFSET`` says how far that clock is from the
start of a provider's trading day, so VWAP restarts where the market's day starts.
"""
from __future__ import annotations

import pandas as pd

IST_OFFSET = 19800      # seconds from UTC to India time

# The timeframes the chart offers. ``seconds`` is a candle's nominal length (a week is seven days).
TIMEFRAMES = {
    "1m": {"label": "1 minute", "seconds": 60},
    "5m": {"label": "5 minutes", "seconds": 300},
    "15m": {"label": "15 minutes", "seconds": 900},
    "1h": {"label": "1 hour", "seconds": 3600},
    "4h": {"label": "4 hours", "seconds": 14400},
    "1d": {"label": "1 day", "seconds": 86400},
    "1w": {"label": "1 week", "seconds": 604800},
}

MAX_BARS = 1500


class MarketDataError(Exception):
    """A provider could not serve candles. The message is shown to the user as it is."""

    def __init__(self, message: str, provider: str = "", needs_login: bool = False):
        super().__init__(message)
        self.provider = provider
        self.needs_login = needs_login


def check_timeframe(timeframe: str) -> str:
    if timeframe not in TIMEFRAMES:
        raise ValueError(f"timeframe must be one of {', '.join(TIMEFRAMES)}")
    return timeframe


def rows_from_frame(frame: pd.DataFrame, limit: int | None = MAX_BARS) -> list[dict]:
    """Bars with an IST wall-clock index as chart rows. Rows with a missing or non-positive price are dropped.
    The newest ``limit`` are kept (``None`` keeps all, for a replay window)."""
    out = []
    for stamp, row in frame.iterrows():
        o, h, low, c = (float(row[k]) for k in ("Open", "High", "Low", "Close"))
        if not all(v > 0 for v in (o, h, low, c)) or any(v != v for v in (o, h, low, c)):
            continue
        volume = float(row.get("Volume", 0) or 0)
        out.append({"time": int(pd.Timestamp(stamp).value // 1_000_000_000), "open": o, "high": h, "low": low,
                    "close": c, "volume": volume if volume == volume else 0.0})
    return out if limit is None else out[-limit:]


def weekly(frame: pd.DataFrame) -> pd.DataFrame:
    """Daily bars merged into weeks that start on Monday, labelled with the week's Monday."""
    if frame.empty:
        return frame
    days = frame.index.normalize()
    monday = days - pd.to_timedelta(days.weekday, unit="D")
    how = {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}
    out = frame[list(how)].groupby(pd.DatetimeIndex(monday)).agg(how)
    return out


def merge_rows(rows: list[dict], seconds: int, anchor: int = 0) -> list[dict]:
    """Chart rows merged into ``seconds``-long candles whose starts are ``anchor`` plus a multiple of ``seconds``."""
    out: list[dict] = []
    for row in rows:
        start = row["time"] - (row["time"] - anchor) % seconds
        if out and out[-1]["time"] == start:
            last = out[-1]
            last["high"] = max(last["high"], row["high"])
            last["low"] = min(last["low"], row["low"])
            last["close"] = row["close"]
            last["volume"] += row["volume"]
        else:
            out.append({**row, "time": start})
    return out
