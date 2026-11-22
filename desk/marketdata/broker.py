"""NSE/BSE candles from the broker the user connected (Kite Connect, or another broker's API on TradeSetu).

Only the broker: no Yahoo fallback. 4-hour candles are built from hourly ones (09:15 and 13:15, as the
exchange's session splits) and weekly candles from daily ones, because the broker serves neither.
"""
from __future__ import annotations

from .. import data
from . import MAX_BARS, TIMEFRAMES, MarketDataError, check_timeframe, rows_from_frame, weekly

# The chart's timeframe -> (the broker interval the desk fetches, minutes to merge into or "week").
BASE = {"1m": ("1m", None), "5m": ("5m", None), "15m": ("15m", None), "1h": ("1h", None),
        "4h": ("1h", 240), "1d": ("1d", None), "1w": ("1d", "week")}

SESSION_OPEN = 9 * 3600 + 15 * 60      # intraday candles start at 09:15 IST, as the exchange's do
ANCHOR = {"1d": 0, "1w": 4 * 86400}     # days start at midnight; weeks on Monday (1970-01-05)


def candles(kite, instrument: str, timeframe: str, now, raw: bool, broker: str = "Kite") -> dict:
    """``instrument`` is EXCHANGE:NAME when ``raw``, else a desk symbol (RELIANCE, TCS.NS)."""
    check_timeframe(timeframe)
    if not kite.connected:
        raise MarketDataError(f"NSE and BSE charts come from your broker. Open Trade, press Connect and log in to "
                              f"{broker}.", broker, needs_login=True)
    interval, merge = BASE[timeframe]
    try:
        frame = data.kite_bars(kite, instrument, interval, now, raw=raw)
    except data.NoData as exc:
        raise MarketDataError(f"{broker} has no candles for this: {exc}", broker) from exc
    except Exception as exc:  # noqa: BLE001 - KiteError and transport failures, shown as they are
        login = getattr(exc, "needs_login", False)
        reason = f"Log in to {broker} on the Trade tab." if login else str(exc)[:200]
        raise MarketDataError(f"{broker} could not serve this chart. {reason}", broker, needs_login=login) from exc
    if merge == "week":
        frame = weekly(frame)
    elif merge:
        frame = data.resample(frame, merge)
    rows = rows_from_frame(frame)
    if not rows:
        raise MarketDataError(f"{broker} returned no usable {TIMEFRAMES[timeframe]['label']} candles.", broker)
    return {"candles": rows[-MAX_BARS:], "source": broker, "notes": [], "session_offset": 0,
            "seconds": TIMEFRAMES[timeframe]["seconds"], "anchor": ANCHOR.get(timeframe, SESSION_OPEN)}
