"""Sector cards: an index for each group of stocks, its trend, and what moves it.

A sector is a named list of NSE stocks with a weight each (equal by default).
Its index starts at 100 a year back. Each day it moves by the weighted average
of its stocks' moves: the close by their close-to-close returns, and the open,
high and low by each stock's open, high and low against its previous close.
That gives the index a candle per day. The high and low are an approximation:
the stocks did not all touch their highs at the same moment.

Prices: a year of daily candles from the price feed (adjusted for splits and
bonuses), with today's candle following live quotes, from Kite when it is
connected, refreshed every few minutes during market hours.

The default sectors are a curated starting set of liquid NSE stocks, not an
exchange classification; every list and weight is editable.
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
import threading
import time
from pathlib import Path

import numpy as np
import pandas as pd

from tradingagents.dataflows.symbols import kite_instrument, normalize_symbol, safe_ticker_component

from . import data, market, trend
from .settings import home

logger = logging.getLogger(__name__)

MAX_SECTORS = 50
MAX_STOCKS = 60
WINDOW = 250              # trading days of index history
LIVE_SECONDS = 180        # how often live prices are refreshed in market hours
HISTORY_SECONDS = 6 * 3600
CARD_CANDLES = 45
DEFAULT_FILE = Path(__file__).with_name("sectors_default.json")
OHLC = ["Open", "High", "Low", "Close"]


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def trend_of(series, rule: dict | None = None) -> str:
    return trend.classify(series, rule)


def change(series: pd.Series, days: int) -> float | None:
    if len(series) <= days:
        return None
    return round((float(series.iloc[-1]) / float(series.iloc[-1 - days]) - 1) * 100, 2)


def candles(frame: pd.DataFrame, count: int | None = None) -> list[dict]:
    view = frame if count is None else frame.iloc[-count:]
    return [{"time": stamp.strftime("%Y-%m-%d"), "open": round(float(r.Open), 2), "high": round(float(r.High), 2),
             "low": round(float(r.Low), 2), "close": round(float(r.Close), 2)} for stamp, r in zip(view.index, view.itertuples(), strict=True)]


def _default_sectors() -> dict:
    raw = json.loads(DEFAULT_FILE.read_text(encoding="utf-8"))
    out = {}
    for name, symbols in raw.items():
        stocks = {normalize_symbol(f"NSE:{s}"): 1.0 for s in symbols.split()}
        out[slug(name)] = {"name": name, "stocks": stocks}
    return out


def yahoo_quotes(symbols: list[str]) -> dict[str, dict]:
    """Today's open, high, low, latest price and previous close of many symbols, in a few requests."""
    import yfinance as yf

    out = {}
    for start in range(0, len(symbols), 100):
        chunk = symbols[start:start + 100]
        try:
            frame = yf.download(tickers=chunk, period="5d", interval="1d", group_by="ticker", auto_adjust=True,
                                threads=False, progress=False)
        except Exception as exc:  # noqa: BLE001 - a failed batch leaves those prices as they were
            logger.warning("live price batch failed: %s", exc)
            continue
        for symbol in chunk:
            with contextlib.suppress(KeyError, IndexError, TypeError, ValueError):
                rows = (frame[symbol] if len(chunk) > 1 else frame)[OHLC].dropna()
                if len(rows) and float(rows["Close"].iloc[-1]) > 0:
                    last = rows.iloc[-1]
                    out[symbol] = {"date": rows.index[-1].strftime("%Y-%m-%d"), "open": float(last.Open),
                                   "high": float(last.High), "low": float(last.Low), "last": float(last.Close),
                                   "prev_close": float(rows["Close"].iloc[-2]) if len(rows) > 1 else None}
    return out


def kite_quotes(kite, symbols: list[str], today: str) -> dict[str, dict]:
    """The same from Kite's quote API (live), up to 500 instruments a call."""
    pairs = {s: "{}:{}".format(*kite_instrument(s)) for s in symbols if kite_instrument(s)}
    names, found = list(pairs.values()), {}
    for start in range(0, len(names), 500):
        found.update(kite.quotes(names[start:start + 500]))
    out = {}
    for symbol, name in pairs.items():
        quote = found.get(name)
        if not quote or not quote.get("last_price"):
            continue
        day = quote.get("ohlc") or {}
        out[symbol] = {"date": today, "open": float(day.get("open") or quote["last_price"]),
                       "high": float(day.get("high") or quote["last_price"]), "low": float(day.get("low") or quote["last_price"]),
                       "last": float(quote["last_price"]), "prev_close": float(day["close"]) if day.get("close") else None,
                       "volume": quote.get("volume"), "quote": quote}
    return out


class Sectors:
    def __init__(self, kite=None, path: Path | None = None, fetch=None, quotes=None, now=market.now_ist, settings=None):
        self.kite, self.fetch, self._now, self.settings = kite, fetch, now, settings
        self._quotes = quotes                      # (symbols) -> {symbol: price or quote dict}; replaces the live lookup
        self.path = path or home() / "sectors.json"
        self._lock = threading.RLock()
        self._sectors: dict[str, dict] = {}
        self._bars: dict[str, pd.DataFrame] = {}   # symbol -> daily OHLC, last WINDOW+60 days
        self._volume: dict[str, pd.Series] = {}    # symbol -> daily traded shares, same days
        self._loaded: dict[str, float] = {}
        self._missing: dict[str, str] = {}
        self._live: dict[str, dict] = {}
        self._live_at: float = 0.0
        self._live_source = ""
        self._progress = {"done": 0, "total": 0, "running": False}
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.extra: list = []                      # more symbols to keep priced (index constituents)
        try:
            stored = json.loads(self.path.read_text(encoding="utf-8"))
            assert isinstance(stored, dict) and stored
            self._sectors = stored
        except (OSError, json.JSONDecodeError, AssertionError):
            self._sectors = _default_sectors()

    @property
    def rule(self) -> dict:
        return self.settings.get("trend_rule") if self.settings is not None else dict(trend.DEFAULT)

    # -- the lists -----------------------------------------------------------

    def _save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._sectors, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    def symbols(self) -> list[str]:
        with self._lock:
            return sorted({s for sector in self._sectors.values() for s in sector["stocks"]})

    def sector_of(self) -> dict[str, list[str]]:
        with self._lock:
            out: dict[str, list[str]] = {}
            for sector in self._sectors.values():
                for symbol in sector["stocks"]:
                    out.setdefault(symbol, []).append(sector["name"])
            return out

    @staticmethod
    def _clean_stocks(raw) -> dict[str, float]:
        if isinstance(raw, str):
            raw = dict.fromkeys(raw.replace(",", " ").split(), 1.0)
        if isinstance(raw, list):
            raw = dict.fromkeys(raw, 1.0)
        if not isinstance(raw, dict) or not raw:
            raise ValueError("A sector needs at least one stock")
        if len(raw) > MAX_STOCKS:
            raise ValueError(f"At most {MAX_STOCKS} stocks in a sector")
        out = {}
        for symbol, weight in raw.items():
            try:
                canonical = safe_ticker_component(normalize_symbol(str(symbol)))
            except ValueError:
                raise ValueError(f"“{str(symbol)[:30]}” is not a valid symbol") from None
            if not kite_instrument(canonical):
                raise ValueError(f"{canonical} is not an NSE/BSE stock")
            if isinstance(weight, bool) or not isinstance(weight, (int, float)) or not 0 <= weight <= 1000 \
                    or weight != weight:
                raise ValueError(f"The weight of {canonical} must be a number from 0 to 1000")
            out[canonical] = float(weight)
        if not any(out.values()):
            raise ValueError("At least one stock needs a weight above zero")
        return out

    def save_sector(self, name: str, stocks, sector_id: str | None = None) -> dict:
        name = " ".join(str(name).split())
        if not 1 <= len(name) <= 40 or not re.fullmatch(r"[\w &,\-./()]+", name):
            raise ValueError("Give the sector a name of up to 40 letters, digits or spaces")
        clean = self._clean_stocks(stocks)
        with self._lock:
            if sector_id is None:
                sector_id = slug(name)
                if not sector_id or sector_id in self._sectors:
                    raise ValueError("A sector with that name already exists")
                if len(self._sectors) >= MAX_SECTORS:
                    raise ValueError(f"The desk shows at most {MAX_SECTORS} sectors. Delete one first.")
            elif sector_id not in self._sectors:
                raise ValueError("no such sector")
            self._sectors[sector_id] = {"name": name, "stocks": clean}
            self._save()
        self.ensure_history(list(clean))
        return self.detail(sector_id)

    def delete(self, sector_id: str) -> None:
        with self._lock:
            if sector_id not in self._sectors:
                raise ValueError("no such sector")
            del self._sectors[sector_id]
            self._save()

    def reset(self) -> None:
        with self._lock:
            self._sectors = _default_sectors()
            self._save()

    # -- prices --------------------------------------------------------------

    def ensure_history(self, symbols: list[str] | None = None, force: bool = False) -> None:
        """Load the daily candles of ``symbols`` (all, by default) that are missing or stale."""
        wanted = symbols if symbols is not None else sorted(set(self.symbols()) | set(self.extra))
        now = time.time()
        todo = [s for s in wanted if force or now - self._loaded.get(s, 0) > HISTORY_SECONDS]
        if symbols is None:
            self._progress.update(done=0, total=len(todo), running=bool(todo))
        for number, symbol in enumerate(todo, 1):
            if self._stop.is_set():
                break
            try:
                frame = data.history(symbol, fetch=self.fetch).iloc[-(WINDOW + 60):]
                bars = frame[OHLC]
                with self._lock:
                    self._bars[symbol] = bars
                    if "Volume" in frame:
                        self._volume[symbol] = frame["Volume"].astype(float)
                    self._missing.pop(symbol, None)
            except data.NoData as exc:
                with self._lock:
                    self._missing[symbol] = str(exc)[:120]
                    self._bars.pop(symbol, None)
            self._loaded[symbol] = time.time()
            if symbols is None:
                self._progress["done"] = number
        if symbols is None:
            self._progress["running"] = False

    def quotes_for(self, symbols: list[str]) -> tuple[dict[str, dict], str]:
        """Live quotes for ``symbols``: Kite's when connected, else the feed's latest."""
        today = self._now().strftime("%Y-%m-%d")
        if self._quotes is not None:
            raw = self._quotes(symbols)
            out = {}
            for symbol, value in raw.items():
                quote = value if isinstance(value, dict) else {"last": float(value)}
                quote.setdefault("date", today)
                for key in ("open", "high", "low"):
                    quote.setdefault(key, quote["last"])
                out[symbol] = quote
            return out, "live"
        if self.kite is not None and self.kite.connected and "get_quotes" in self.kite.tools:
            with contextlib.suppress(Exception):
                found = kite_quotes(self.kite, symbols, today)
                if found:
                    return found, "Kite live prices"
        if self.fetch is None:
            return yahoo_quotes(symbols), "delayed prices"
        return {}, ""

    def refresh_live(self) -> None:
        prices, source = self.quotes_for(sorted(set(self.symbols()) | set(self.extra)))
        if prices:
            with self._lock:
                self._live, self._live_at, self._live_source = prices, time.time(), source

    def bars(self, symbol: str) -> pd.DataFrame | None:
        """A stock's daily candles with today's candle from its live quote."""
        frame = self._bars.get(symbol)
        if frame is None or frame.empty:
            return None
        live = self._live.get(symbol)
        if not live:
            return frame
        now = self._now()
        today = pd.Timestamp(now.date())
        row = pd.DataFrame([[live["open"], max(live["high"], live["last"]), min(live["low"], live["last"]), live["last"]]],
                           columns=OHLC, index=[today])
        if frame.index[-1] == today:
            return pd.concat([frame.iloc[:-1], row])
        if market.is_trading_day(now) and now.time() >= market.OPEN and str(live.get("date", "")) == today.strftime("%Y-%m-%d"):
            return pd.concat([frame, row])
        return frame

    def volume(self, symbol: str) -> tuple[pd.Series | None, float | None]:
        """A stock's daily volume history (completed days) and today's volume so far from its live quote."""
        series = self._volume.get(symbol)
        live = self._live.get(symbol) or {}
        today = self._now().strftime("%Y-%m-%d")
        now_volume = float(live["volume"]) if live.get("volume") and str(live.get("date", "")) == today else None
        if series is not None and len(series) and series.index[-1] == pd.Timestamp(today):
            if now_volume is None:
                now_volume = float(series.iloc[-1])
            series = series.iloc[:-1]
        return series, now_volume

    # -- the index -----------------------------------------------------------

    def index_of(self, stocks: dict[str, float]) -> tuple[pd.DataFrame, dict[str, pd.DataFrame]]:
        """The weighted index of ``stocks`` as daily candles, and the stocks' own candles."""
        series = {s: frame for s in stocks if (frame := self.bars(s)) is not None and len(frame) > 1}
        if not series:
            return pd.DataFrame(columns=OHLC), {}
        close = pd.DataFrame({s: f["Close"] for s, f in series.items()}).sort_index()
        dates = close.index
        # A stock with no price on a day (a halt, or no quote yet) counts as unchanged; one not
        # yet listed stays out until its first price.
        filled = close.ffill()
        previous = filled.shift(1)
        listed = close.notna() | (filled.notna() & previous.notna())
        weights = pd.Series({s: stocks[s] for s in close.columns})
        active = (previous.notna() & listed).mul(weights, axis=1)
        total = active.sum(axis=1).replace(0, np.nan)

        def move(field):
            values = pd.DataFrame({s: f[field] for s, f in series.items()}).reindex(dates)
            values = values.where(close.notna(), filled)       # a missing day moves nothing
            ratio = values / previous - 1
            return (ratio.fillna(0).mul(active).sum(axis=1) / total).fillna(0)

        moves = {field: move(field) for field in OHLC}
        level = 100 * (1 + moves["Close"]).cumprod()
        before = level.shift(1).fillna(100.0)
        frame = pd.DataFrame({field: before * (1 + moves[field]) for field in OHLC})
        frame.iloc[0] = 100.0
        frame["High"] = frame[OHLC].max(axis=1)
        frame["Low"] = frame[OHLC].min(axis=1)
        frame = frame.iloc[-(WINDOW + 1):]
        frame = frame / float(frame["Close"].iloc[0]) * 100       # the window starts at 100
        return frame, series

    def _when(self) -> dict:
        return {"live_source": self._live_source or "last close",
                "updated_ist": self._now().strftime("%Y-%m-%d %H:%M") if self._live_at else None}

    def summary(self, index: pd.DataFrame, series: dict) -> dict:
        rule = self.rule
        close = index["Close"]
        above = [trend.classify(line, rule) == "Uptrend" for line in series.values() if len(line) >= trend.needed(rule)]
        return {"trend": trend.classify(index, rule), "value": round(float(close.iloc[-1]), 2),
                "change_1d": change(close, 1), "change_1w": change(close, 5), "change_1m": change(close, 21),
                "change_3m": change(close, 63), "change_1y": round(float(close.iloc[-1]) - 100, 2),
                "breadth": round(sum(above) / len(above) * 100) if above else None,
                "candles": candles(index, CARD_CANDLES)}

    def cards(self) -> dict:
        with self._lock:
            out = []
            for sector_id, sector in self._sectors.items():
                index, series = self.index_of(sector["stocks"])
                card = {"id": sector_id, "name": sector["name"], "stocks": len(sector["stocks"]), "priced": len(series)}
                card.update(self.summary(index, series) if len(index) > 1 else {"trend": "Loading", "candles": []})
                out.append(card)
            return {"sectors": out, "progress": dict(self._progress), "max": MAX_SECTORS, **self._when(),
                    "session_open": market.session_status(self._now())["open"],
                    "trend_rule": self.rule, "trend_text": trend.describe(self.rule)}

    def stock_rows(self, stocks: dict[str, float], series: dict, fundamentals=None) -> list[dict]:
        rule = self.rule
        total_weight = sum(w for s, w in stocks.items() if s in series) or 1.0
        rows = []
        for symbol, weight in stocks.items():
            frame = series.get(symbol)
            row = {"symbol": symbol, "weight": weight, "priced": frame is not None, "note": self._missing.get(symbol)}
            if frame is not None:
                line = frame["Close"]
                day = change(line, 1)
                share = weight / total_weight
                year = frame.iloc[-WINDOW:]
                row.update(price=round(float(line.iloc[-1]), 2), change_1d=day, change_1w=change(line, 5),
                           change_1m=change(line, 21), change_3m=change(line, 63),
                           change_1y=change(line, min(len(line) - 1, WINDOW)),
                           trend=trend.classify(frame, rule), weight_pct=round(share * 100, 2),
                           contribution_1d=round(day * share, 3) if day is not None else None,
                           high_52w=round(float(year["High"].max()), 2), low_52w=round(float(year["Low"].min()), 2))
            if fundamentals is not None:
                facts = fundamentals.get(symbol) or {}
                row.update(market_cap=facts.get("market_cap"), pe=facts.get("pe"), roe=facts.get("roe"))
            rows.append(row)
        rows.sort(key=lambda r: (r.get("contribution_1d") is None, -(r.get("contribution_1d") or 0)))
        return rows

    def detail(self, sector_id: str, fundamentals=None) -> dict:
        with self._lock:
            sector = self._sectors.get(sector_id)
            if not sector:
                raise ValueError("no such sector")
            index, series = self.index_of(sector["stocks"])
            out = {"id": sector_id, "name": sector["name"], "trend": "Loading", "candles": [], "points": [],
                   "stocks": self.stock_rows(sector["stocks"], series, fundamentals), **self._when(),
                   "trend_text": trend.describe(self.rule)}
            if len(index) > 1:
                out.update(self.summary(index, series))
                out["candles"] = candles(index)
                out["points"] = [{"time": c["time"], "value": c["close"]} for c in out["candles"]]
            return out

    # -- keeping it fresh ----------------------------------------------------

    def tick(self) -> None:
        """One refresh: stale histories, then live prices when the market is open or none are held yet."""
        self.ensure_history()
        open_now = market.session_status(self._now())["open"]
        if not self._live_at or (open_now and time.time() - self._live_at >= LIVE_SECONDS):
            self.refresh_live()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()

        def loop():
            while not self._stop.is_set():
                try:
                    self.tick()
                except Exception:  # noqa: BLE001 - keep refreshing
                    logger.exception("sector refresh failed")
                self._stop.wait(60)

        self._thread = threading.Thread(target=loop, name="desk-sectors", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
