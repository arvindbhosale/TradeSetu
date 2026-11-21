"""Daily price history for charts and backtests, adjusted for splits and bonuses."""

from __future__ import annotations

import logging
import time
from pathlib import Path

import numpy as np
import pandas as pd

from tradingagents.dataflows.symbols import normalize_symbol, safe_ticker_component

from .settings import home

logger = logging.getLogger(__name__)

COLUMNS = ["Open", "High", "Low", "Close", "Volume"]
_CACHE_SECONDS = 6 * 3600
_THIN = 30     # fewer daily bars than this is not a history

# Candle sizes, with how far back Yahoo serves each and how long a download stays fresh. ``kite`` is the
# historical-data API's own interval name; where Kite (or Yahoo) has no such size, ``resample`` names the
# finer candle it is built from (see ``resample``).
INTERVALS = {
    "1m": {"label": "1 minute", "yahoo": "1m", "period": "7d", "fresh": 45, "minutes": 1, "kite": "minute"},
    "2m": {"label": "2 minutes", "yahoo": "2m", "period": "60d", "fresh": 45, "minutes": 2, "kite": "minute",
           "kite_resample": True},
    "3m": {"label": "3 minutes", "yahoo": "1m", "period": "7d", "fresh": 45, "minutes": 3, "kite": "3minute",
           "yahoo_resample": True},
    "4m": {"label": "4 minutes", "yahoo": "1m", "period": "7d", "fresh": 45, "minutes": 4, "kite": "minute",
           "kite_resample": True, "yahoo_resample": True},
    "5m": {"label": "5 minutes", "yahoo": "5m", "period": "60d", "fresh": 60, "minutes": 5, "kite": "5minute"},
    "15m": {"label": "15 minutes", "yahoo": "15m", "period": "60d", "fresh": 90, "minutes": 15, "kite": "15minute"},
    "30m": {"label": "30 minutes", "yahoo": "30m", "period": "60d", "fresh": 120, "minutes": 30, "kite": "30minute"},
    "1h": {"label": "1 hour", "yahoo": "60m", "period": "730d", "fresh": 300, "minutes": 60, "kite": "60minute"},
    "1d": {"label": "1 day", "yahoo": "1d", "period": "max", "fresh": _CACHE_SECONDS, "minutes": 375, "kite": "day"},
}
INTRADAY = tuple(key for key in INTERVALS if key != "1d")
SESSION_OPEN = pd.Timedelta(hours=9, minutes=15)      # NSE/BSE; MCX and currency open earlier, at 09:00


def resample(frame: pd.DataFrame, minutes: int) -> pd.DataFrame:
    """Intraday bars merged into ``minutes``-long candles, aligned to the session open as Kite aligns its own.

    Each day's buckets start at 09:15 (NSE/BSE), or at the day's first bar when it trades earlier (MCX at
    09:00), so a 4-minute series reads 09:15, 09:19, ... like the exchange's. A bucket the session or the
    data cuts short (NSE's 15:27 4-minute candle has 3 minutes; the newest one may still be forming) is kept
    with what it has: callers that need finished candles drop the last by its nominal end, as for any size.
    """
    if frame.empty or minutes <= 1:
        return frame
    index = frame.index
    day = index.normalize()
    clock = pd.Series(index - day, index=index)
    anchor = clock.groupby(day).transform("min").clip(upper=SESSION_OPEN)
    step = pd.Timedelta(minutes=minutes)
    stamp = day + anchor.to_numpy() + ((clock - anchor) // step).to_numpy() * step
    how = {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}
    how.update({c: "last" for c in frame.columns if c not in how})
    out = frame.groupby(pd.DatetimeIndex(stamp, name=index.name)).agg(how)
    return out[list(frame.columns)]


class NoData(Exception):
    """No usable price history for a symbol."""


def _fetch_yahoo(symbol: str, interval: str = "1d") -> pd.DataFrame:
    import yfinance as yf

    from tradingagents.dataflows.vendors.yahoo.common import yf_retry

    spec = INTERVALS[interval]
    # auto_adjust folds splits, bonuses and dividends into the prices: an
    # unadjusted 1:1 bonus reads as a 50% crash and fires every stoploss.
    return yf_retry(lambda: yf.Ticker(symbol).history(period=spec["period"], interval=spec["yahoo"],
                                                      auto_adjust=True))


def _clean(frame: pd.DataFrame | None, intraday: bool = False) -> pd.DataFrame:
    if frame is None or frame.empty or not set(COLUMNS) <= set(frame.columns):
        return pd.DataFrame(columns=COLUMNS)
    frame = frame[COLUMNS].copy()
    index = pd.to_datetime(frame.index)
    if getattr(index, "tz", None) is not None:
        index = index.tz_localize(None)      # the bar's own market date and time (IST for NSE/BSE)
    frame.index = index if intraday else index.normalize()
    frame.index.name = "Date"
    frame = frame[~frame.index.duplicated(keep="last")].sort_index()
    frame = frame.dropna(subset=["Open", "High", "Low", "Close"])
    frame = frame[(frame[["Open", "High", "Low", "Close"]] > 0).all(axis=1)]
    return frame if intraday else _mend_bad_prints(frame)


def _mend_bad_prints(frame: pd.DataFrame) -> pd.DataFrame:
    """Scale back days the feed printed at 1/N of the price (N a whole number ≥ 2) when the price returns
    within 5 days: Yahoo has NIFTYBEES at 1/10 and GOLDBEES at 1/100 on 19-20 Dec 2019. A drop that does not
    come back is left alone (it may be real, or a split the feed has not adjusted)."""
    close = frame["Close"].to_numpy(float, copy=True)
    if len(close) < 3:
        return frame
    ratio = close[:-1] / close[1:]
    suspects = [i + 1 for i in range(len(ratio)) if ratio[i] >= 1.9]
    if not suspects:
        return frame
    frame = frame.copy()
    cols = ["Open", "High", "Low", "Close"]
    for i in suspects:
        whole = round(close[i - 1] / close[i])
        if whole < 2 or abs(close[i - 1] / close[i] / whole - 1) >= 0.06:
            continue
        back = next((j for j in range(i + 1, min(i + 6, len(close))) if abs(close[j] / close[j - 1] / whole - 1) < 0.06), None)
        if back is not None:
            frame.iloc[i:back, [frame.columns.get_loc(c) for c in cols]] *= whole
            close[i:back] *= whole
    return frame


def candles(symbol: str, interval: str, fetch=None, cache_dir: Path | None = None,
            max_age: float | None = None) -> pd.DataFrame:
    """Intraday candles (1m to 1h) as far back as the feed serves, times in IST.

    Yahoo's intraday prices for NSE/BSE are delayed and unadjusted history is
    short (7 days of 1-minute bars, so of 3 and 4-minute too, which are built
    from them; 60 days of 2 to 30-minute; 2 years of hourly).
    ``fetch(symbol, interval)`` replaces the download.
    """
    if interval not in INTERVALS or interval == "1d":
        raise ValueError(f"unknown intraday interval: {interval}")
    canonical = normalize_symbol(symbol)
    if fetch is not None:
        frame = _clean(fetch(canonical, interval), intraday=True)
    else:
        cache_dir = cache_dir or home() / "prices"
        cache_dir.mkdir(parents=True, exist_ok=True)
        path = cache_dir / f"{safe_ticker_component(canonical)}.{interval}.csv"
        fresh = INTERVALS[interval]["fresh"] if max_age is None else max_age
        frame = None
        if path.exists() and time.time() - path.stat().st_mtime < fresh:
            try:
                frame = _clean(pd.read_csv(path, index_col=0, parse_dates=True), intraday=True)
            except (OSError, ValueError):
                frame = None
        if frame is None or frame.empty:
            try:
                frame = _clean(_fetch_yahoo(canonical, interval), intraday=True)
                if INTERVALS[interval].get("yahoo_resample"):
                    frame = resample(frame, INTERVALS[interval]["minutes"])
            except Exception as exc:  # noqa: BLE001 - any vendor failure is "no data"
                raise NoData(f"could not load {interval} candles for {canonical}: {exc}") from exc
            if not frame.empty:
                frame.to_csv(path)
    if frame.empty:
        raise NoData(f"no {INTERVALS[interval]['label']} candles for {canonical}. Intraday data covers NSE "
                     f"symbols and indices for a limited period only.")
    return frame


# How an index is named on Kite, by its Yahoo symbol.
KITE_INDEX = {
    "^NSEI": "NSE:NIFTY 50", "^NSEBANK": "NSE:NIFTY BANK", "^BSESN": "BSE:SENSEX", "^INDIAVIX": "NSE:INDIA VIX",
    "^CNXIT": "NSE:NIFTY IT", "NIFTY_FIN_SERVICE.NS": "NSE:NIFTY FIN SERVICE", "^NSMIDCP": "NSE:NIFTY NEXT 50",
    "^CRSLDX": "NSE:NIFTY 500", "^CNXAUTO": "NSE:NIFTY AUTO", "^CNXPHARMA": "NSE:NIFTY PHARMA",
    "^CNXFMCG": "NSE:NIFTY FMCG", "^CNXMETAL": "NSE:NIFTY METAL", "^CNXREALTY": "NSE:NIFTY REALTY",
    "^CNXENERGY": "NSE:NIFTY ENERGY", "^CNXPSUBANK": "NSE:NIFTY PSU BANK",
}
# Days of history asked of Kite per candle size (within its per-request limits).
KITE_LOOKBACK = {"1m": 14, "2m": 14, "3m": 30, "4m": 20, "5m": 60, "15m": 120, "30m": 180, "1h": 365, "1d": 1990}
_kite_cache: dict = {}


def kite_instrument_for(symbol: str) -> str | None:
    """``EXCHANGE:NAME`` as Kite spells a stock or index, or None when Kite has no such instrument."""
    from tradingagents.dataflows.symbols import kite_instrument

    canonical = normalize_symbol(symbol)
    if canonical in KITE_INDEX:
        return KITE_INDEX[canonical]
    pair = kite_instrument(canonical)
    return f"{pair[0]}:{pair[1]}" if pair else None


def kite_frame(rows: list) -> pd.DataFrame:
    """Kite's candle rows as bars with IST wall-clock times."""
    frame = pd.DataFrame(rows)
    if frame.empty or "date" not in frame:
        return pd.DataFrame(columns=COLUMNS)
    stamps = pd.to_datetime(frame["date"])
    if getattr(stamps.dt, "tz", None) is not None:
        stamps = stamps.dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
    frame.index = pd.DatetimeIndex(stamps)
    frame = frame.rename(columns=str.capitalize)
    if "Volume" not in frame:
        frame["Volume"] = 0.0           # indices carry no volume
    frame = frame[COLUMNS].astype(float)
    return frame[~frame.index.duplicated(keep="last")].sort_index()


def kite_bars(kite, symbol: str, interval: str, now, raw: bool = False) -> pd.DataFrame:
    """Candles for ``symbol`` from Kite's historical-data API. Raises NoData or KiteError.

    Needs a Kite Connect key with market data (the paid Connect plan; the free
    Personal plan has none). Daily candles from Kite are not adjusted for
    splits and bonuses.
    """
    instrument = symbol if raw else kite_instrument_for(symbol)
    if not instrument:
        raise NoData(f"Kite has no instrument for {normalize_symbol(symbol)}.")
    key, ttl = (instrument, interval), (60 if interval == "1d" else 12)
    cached = _kite_cache.get(key)
    if cached and time.time() - cached[0] < ttl:
        return cached[1]
    naive = now.replace(tzinfo=None)
    # A futures contract lives about three months; for day candles Kite stitches the expired ones in (continuous).
    continuous = interval == "1d" and instrument.split(":", 1)[0] in ("NFO", "BFO", "MCX") and instrument.endswith("FUT")
    if continuous:
        rows = kite.candles(instrument, interval, naive - pd.Timedelta(days=KITE_LOOKBACK[interval]), naive, continuous=True)
    else:
        rows = kite.candles(instrument, interval, naive - pd.Timedelta(days=KITE_LOOKBACK[interval]), naive)
    frame = kite_frame(rows)
    if interval == "1d":
        frame.index = frame.index.normalize()
    if frame.empty:
        raise NoData(f"Kite returned no {INTERVALS[interval]['label']} candles for {instrument}.")
    _kite_cache[key] = (time.time(), frame)
    return frame


# Kite's limit on the span of one historical request, in days (its "interval exceeds max limit" error). 2 and
# 4-minute candles are built from minute candles, so they take the minute limit.
KITE_CHUNK = {"1m": 60, "2m": 60, "3m": 100, "4m": 60, "5m": 100, "15m": 200, "30m": 200, "1h": 400, "1d": 2000}
# Kite keeps minute-level candles (every intraday size) from early 2015; day candles go back further. This is
# Kite's limit, not the desk's: an intraday range that starts earlier is tested from here.
KITE_INTRADAY_FROM = pd.Timestamp("2015-02-02")
_range_cache: dict = {}
_RANGE_CACHE_SIZE = 6

# Price ratios a split or bonus leaves between one day's close and the next day's open (1:1 bonus 2, 1:2 bonus
# 1.5, 1:4 bonus 1.25, ten-for-one split 10). Under 1.4 a ratio is too close to a real fall (a stock opening at
# its 20% lower circuit reads 1.25), so those are adjusted only when the adjusted feed shows no such fall.
_SPLIT_RATIOS = (1.2, 1.25, 4 / 3, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 6.0, 8.0, 10.0, 20.0, 25.0, 50.0, 100.0)


def adjust_splits(frame: pd.DataFrame, reference=None) -> tuple[pd.DataFrame, list[tuple[str, float]]]:
    """Bars with unadjusted splits and bonuses folded back into the earlier prices, and the gaps it adjusted.

    A day that opens at 1/N of the previous close, N one of the usual split or bonus ratios (within 3%), is
    read as a corporate action: every earlier price is divided by N (volume multiplied), so the gap does not
    fire a stoploss that was never hit. ``reference()`` may return adjusted daily bars (Yahoo's) to check each
    gap against: the same gap there is a real fall and is left alone; none there confirms the corporate action.
    A ratio under 1.4 is adjusted only when confirmed. Already adjusted data has no such gaps and is unchanged.
    """
    if len(frame) < 2:
        return frame, []
    day = frame.index.normalize()
    starts = np.flatnonzero(np.r_[False, day[1:] != day[:-1]])
    closes, opens = frame["Close"].to_numpy(float), frame["Open"].to_numpy(float)
    found, ref, looked = [], None, False
    for i in starts:
        ratio = closes[i - 1] / opens[i]
        match = next((r for r in _SPLIT_RATIOS if abs(ratio / r - 1) < 0.03), None) if ratio >= 1.15 else None
        if not match:
            continue
        if not looked:
            looked = True
            try:
                ref = reference() if reference else None
            except Exception:  # noqa: BLE001 - no reference is "unconfirmed", not a failure
                ref = None
        verdict = None
        if ref is not None and len(ref):
            before, on = ref[ref.index < day[i]], ref[ref.index == day[i]]
            if len(before) and len(on):
                verdict = float(before["Close"].iloc[-1]) / float(on["Open"].iloc[0]) < 1.1   # no gap: a split
        if verdict or (verdict is None and match >= 1.4):
            found.append((i, match))
    if not found:
        return frame, []
    frame = frame.copy()
    prices = [frame.columns.get_loc(c) for c in ("Open", "High", "Low", "Close")]
    for i, ratio in found:
        frame.iloc[:i, prices] /= ratio
        if "Volume" in frame:
            frame.iloc[:i, frame.columns.get_loc("Volume")] *= ratio
    return frame, [(frame.index[i].strftime("%d %b %Y"), ratio) for i, ratio in found]


def kite_range(kite, symbol: str, interval: str, start: str, end: str, raw: bool = False,
               continuous: bool = False, reference=None) -> pd.DataFrame:
    """Kite candles between two dates (inclusive), fetched in as many requests as Kite's limits need.

    Intraday history on Kite reaches back to 2015, against days or weeks on the free feed, which is what makes
    an intraday backtest worth running. ``raw`` takes ``symbol`` as Kite's own ``EXCHANGE:NAME``. A stock's
    splits and bonuses are adjusted (``adjust_splits``). ``frame.attrs`` carries ``from_clipped`` when the
    range started before Kite's intraday history, and ``adjusted`` (the corporate-action gaps folded back).
    ``reference`` is passed to ``adjust_splits``.
    """
    instrument = symbol if raw else kite_instrument_for(symbol)
    if not instrument:
        raise NoData(f"Kite has no instrument for {normalize_symbol(symbol)}.")
    first, last = pd.Timestamp(start), pd.Timestamp(end) + pd.Timedelta(hours=23, minutes=59)
    clipped = None
    if interval != "1d" and first < KITE_INTRADAY_FROM:
        clipped, first = first, KITE_INTRADAY_FROM
    if first > last:
        raise NoData(f"Kite keeps {INTERVALS[interval]['label']} candles from {KITE_INTRADAY_FROM:%d %b %Y} only.")
    key = (instrument, interval, first, last, continuous)
    hit = _range_cache.get(key)
    # A finished range is kept 30 minutes (re-running with other exits fetches nothing); one that reaches today
    # keeps growing during the session, so it is kept a minute.
    ttl = 60 if last.normalize() >= pd.Timestamp.now().normalize() else 1800
    if hit and time.time() - hit[0] < ttl:
        return hit[1].copy()
    frames, cursor = [], first
    while cursor <= last:
        stop = min(cursor + pd.Timedelta(days=KITE_CHUNK[interval]) - pd.Timedelta(seconds=1), last)
        if continuous:
            rows = kite.candles(instrument, interval, cursor.to_pydatetime(), stop.to_pydatetime(), continuous=True)
        else:
            rows = kite.candles(instrument, interval, cursor.to_pydatetime(), stop.to_pydatetime())
        frames.append(kite_frame(rows))
        cursor = stop + pd.Timedelta(seconds=1)
        if cursor <= last:
            time.sleep(0.35)         # Kite allows three historical requests a second
    frame = pd.concat([f for f in frames if not f.empty]) if any(not f.empty for f in frames) \
        else pd.DataFrame(columns=COLUMNS)
    frame = frame[~frame.index.duplicated(keep="last")].sort_index()
    if interval == "1d":
        frame.index = frame.index.normalize()
    if frame.empty:
        raise NoData(f"Kite returned no {INTERVALS[interval]['label']} candles for {instrument} in that range.")
    adjusted = []
    exchange = instrument.split(":", 1)[0]
    if exchange in ("NSE", "BSE") and instrument not in KITE_INDEX.values():
        frame, adjusted = adjust_splits(frame, reference)
    frame.attrs.update(from_clipped=clipped, adjusted=adjusted)
    _range_cache[key] = (time.time(), frame)
    while len(_range_cache) > _RANGE_CACHE_SIZE:
        _range_cache.pop(next(iter(_range_cache)))
    return frame.copy()


def bars_for(symbol: str, interval: str = "1d", start: str | None = None, end: str | None = None,
             fetch=None, max_age: float | None = None) -> pd.DataFrame:
    """Daily or intraday bars for ``symbol``, trimmed to ``start``..``end`` (inclusive dates)."""
    if interval == "1d":
        kwargs = {} if max_age is None else {"max_age": max_age}
        return history(symbol, start, end, fetch=fetch, **kwargs)
    frame = candles(symbol, interval, fetch=fetch, max_age=max_age)
    if start:
        frame = frame[frame.index >= pd.Timestamp(start)]
    if end:
        frame = frame[frame.index < pd.Timestamp(end) + pd.Timedelta(days=1)]
    if frame.empty:
        raise NoData(f"no {interval} candles for {normalize_symbol(symbol)} between {start} and {end}. "
                     f"{INTERVALS[interval]['label'].capitalize()} candles reach back {INTERVALS[interval]['period']} only.")
    return frame


def history(symbol: str, start: str | None = None, end: str | None = None,
            fetch=None, cache_dir: Path | None = None, _nse_twin: bool = True,
            max_age: float = _CACHE_SECONDS) -> pd.DataFrame:
    """Adjusted daily bars for ``symbol`` between ``start`` and ``end`` inclusive.

    The full history is cached per symbol for ``max_age`` seconds (a few hours
    by default). ``fetch`` replaces the Yahoo download (tests, or another
    vendor) and bypasses the cache.
    """
    canonical = normalize_symbol(symbol)
    if fetch is not None:
        frame = _clean(fetch(canonical))
    else:
        cache_dir = cache_dir or home() / "prices"
        cache_dir.mkdir(parents=True, exist_ok=True)
        path = cache_dir / f"{safe_ticker_component(canonical)}.csv"
        frame = None
        if path.exists() and time.time() - path.stat().st_mtime < max_age:
            try:
                frame = _clean(pd.read_csv(path, index_col=0, parse_dates=True))
            except (OSError, ValueError):
                frame = None
        if frame is None or frame.empty:
            try:
                frame = _clean(_fetch_yahoo(canonical))
            except Exception as exc:  # noqa: BLE001 - any vendor failure is "no data"
                raise NoData(f"could not load prices for {canonical}: {exc}") from exc
            if not frame.empty:
                frame.to_csv(path)
    if len(frame) < _THIN and canonical.endswith(".BO") and _nse_twin:
        # Yahoo carries little or no history for many BSE listings. The same
        # share trades on NSE at near-identical prices, so its series stands in.
        twin = canonical[:-3] + ".NS"
        try:
            frame = history(twin, fetch=fetch, cache_dir=cache_dir, _nse_twin=False, max_age=max_age)
            frame.attrs["note"] = (f"Yahoo has no usable history for {canonical}; prices shown are the "
                                   f"NSE listing {twin}, which trades within a few paise of it.")
        except NoData:
            pass
    if frame.empty:
        raise NoData(f"no price history for {canonical}. Check the symbol: NSE as RELIANCE or "
                     f"RELIANCE.NS, BSE as BSE:RELIANCE or RELIANCE.BO. BSE scrip codes (500325) "
                     f"are not served by the price feed.")
    note = frame.attrs.get("note")
    if start:
        frame = frame[frame.index >= pd.Timestamp(start)]
    if end:
        frame = frame[frame.index <= pd.Timestamp(end)]
    if frame.empty:
        raise NoData(f"no prices for {canonical} between {start} and {end}")
    if note:
        frame.attrs["note"] = note
    return frame


def last_close(symbol: str, fetch=None) -> float:
    return float(history(symbol, fetch=fetch)["Close"].iloc[-1])
