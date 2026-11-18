"""Five market tools built on the desk's own data.

- **Market Pulse**: one 0-100 reading of the market's fear or greed, from six
  measures (breadth, trend, momentum, volatility, new highs against new lows,
  today's advances), with a heat map of sectors and Nifty 50 stocks.
- **Whale Deals**: NSE's bulk and block deals (big trades by named clients),
  and stocks trading far above their usual volume today.
- **Results Drift**: post-earnings announcement drift. Stocks that reported
  results in the last 60 days (BSE's results calendar), how hard the price
  reacted, and whether it has kept moving the same way since.
- **Sector Compass**: a relative rotation map of the sector indices against
  the Nifty 50 (strength and its momentum, four quadrants, weekly tails).
- **Stock Radar**: one table of every tracked stock with returns, distance from
  its 52-week high, strength against the Nifty, volume, valuation and a score.

Every measure works on daily candles the desk already keeps (``Sectors`` for
stocks, ``Indices`` for indices); only the deals and the results calendar are
downloaded, once a day, from NSE and BSE.
"""

from __future__ import annotations

import contextlib
import csv
import io
import json
import logging
import math
import re
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from tradingagents.dataflows.symbols import normalize_symbol

from . import data, market, trend
from .settings import home

logger = logging.getLogger(__name__)

NSE_DEALS = {"Bulk": "https://nsearchives.nseindia.com/content/equities/bulk.csv",
             "Block": "https://nsearchives.nseindia.com/content/equities/block.csv"}
BSE_RESULTS = "https://api.bseindia.com/BseIndiaAPI/api/Corpforthresults/w"
DEALS_SECONDS = 2 * 3600
RESULTS_SECONDS = 12 * 3600
KEEP_DEALS_DAYS = 30
RESULTS_DAYS = 60
SPIKE = 3.0                       # today's volume at least this many times the 20-day average

ZONES = [(20, "Extreme fear"), (40, "Fear"), (60, "Neutral"), (80, "Greed"), (100.01, "Extreme greed")]


def zone(score: float | None) -> str:
    if score is None:
        return "No reading"
    return next(label for top, label in ZONES if score < top)


def _clip(x: float) -> float:
    return float(min(100.0, max(0.0, x)))


def _pct_rank(series: pd.Series, value: float) -> float:
    """Where ``value`` sits in ``series``, 0-100."""
    clean = series.dropna()
    if clean.empty:
        return 50.0
    return float((clean < value).mean() * 100 + (clean == value).mean() * 50)


# -- Market Pulse --------------------------------------------------------------------------------

def _stock_measures(stocks: dict[str, pd.DataFrame], day: pd.Timestamp) -> dict:
    """Breadth, highs/lows and advances among ``stocks`` as of the close of ``day``."""
    above = counted = highs = lows = ups = movers = 0
    for frame in stocks.values():
        close = frame["Close"]
        close = close[close.index <= day]
        if len(close) < 51:
            continue
        last = close.iloc[-1]
        counted += 1
        above += last > close.iloc[-50:].mean()
        year = close.iloc[-252:]
        if last >= year.max() * 0.98:
            highs += 1
        elif last <= year.min() * 1.02:
            lows += 1
        if close.iloc[-1] != close.iloc[-2]:
            movers += 1
            ups += close.iloc[-1] > close.iloc[-2]
    return {"counted": counted, "above": above, "highs": highs, "lows": lows, "ups": ups, "movers": movers}


def pulse_reading(nifty: pd.DataFrame | None, vix: pd.Series | None, stocks: dict[str, pd.DataFrame],
                  day: pd.Timestamp | None = None) -> dict:
    """The Market Pulse score (0 fear .. 100 greed) and its parts, as of the close of ``day`` (latest by default)."""
    parts = []
    if nifty is not None and len(nifty) > 60:
        close = nifty["Close"]
        day = day if day is not None else close.index[-1]
        close = close[close.index <= day]
        if len(close) >= 200:
            gap = (close.iloc[-1] / close.iloc[-200:].mean() - 1) * 100
            parts.append({"key": "trend", "name": "Nifty against its 200-day average",
                          "score": _clip(50 + gap * 5), "detail": f"{gap:+.1f}% from the 200-day average"})
        if len(close) >= 60:
            moves = close.pct_change(21).dropna().iloc[-250:]
            if len(moves) > 20:
                now = float(moves.iloc[-1])
                parts.append({"key": "momentum", "name": "Nifty one-month return against the past year",
                              "score": _clip(_pct_rank(moves, now)), "detail": f"{now * 100:+.1f}% over 21 sessions"})
    elif stocks:
        day = day if day is not None else max(f.index[-1] for f in stocks.values())
    if vix is not None and len(vix) > 60 and day is not None:
        series = vix[vix.index <= day].dropna().iloc[-250:]
        if len(series) > 20:
            now = float(series.iloc[-1])
            parts.append({"key": "volatility", "name": "India VIX against the past year (high VIX is fear)",
                          "score": _clip(100 - _pct_rank(series, now)), "detail": f"VIX {now:.1f}"})
    if stocks and day is not None:
        m = _stock_measures(stocks, day)
        if m["counted"]:
            parts.append({"key": "breadth", "name": "Stocks above their 50-day average",
                          "score": _clip(m["above"] / m["counted"] * 100), "detail": f"{m['above']} of {m['counted']}"})
            total = m["highs"] + m["lows"]
            parts.append({"key": "highs", "name": "Near 52-week highs against near 52-week lows",
                          "score": _clip(m["highs"] / total * 100) if total else 50.0,
                          "detail": f"{m['highs']} near highs, {m['lows']} near lows"})
        if m["movers"]:
            parts.append({"key": "advances", "name": "Stocks up on the day",
                          "score": _clip(m["ups"] / m["movers"] * 100), "detail": f"{m['ups']} up of {m['movers']}"})
    score = round(sum(p["score"] for p in parts) / len(parts), 1) if parts else None
    for p in parts:
        p["score"] = round(p["score"], 1)
    return {"score": score, "zone": zone(score), "parts": parts,
            "date": day.strftime("%Y-%m-%d") if day is not None else None}


def pulse(nifty, vix, stocks, history_days: int = 60) -> dict:
    """Today's reading, the daily score over ``history_days`` sessions, and the readings a week and a month ago."""
    now = pulse_reading(nifty, vix, stocks)
    out = {**now, "history": [], "week_ago": None, "month_ago": None}
    if nifty is None or len(nifty) < 61 or now["score"] is None:
        return out
    days = nifty.index[-history_days:]
    history = []
    for day in days:
        reading = pulse_reading(nifty, vix, stocks, day)
        if reading["score"] is not None:
            history.append({"date": day.strftime("%Y-%m-%d"), "score": reading["score"]})
    if history:
        history[-1]["score"] = now["score"]
    out["history"] = history
    if len(history) > 5:
        out["week_ago"] = history[-6]["score"]
    if len(history) > 21:
        out["month_ago"] = history[-22]["score"]
    return out


# -- Whale Deals ---------------------------------------------------------------------------------

def _number(text) -> float | None:
    with contextlib.suppress(TypeError, ValueError):
        return float(str(text).replace(",", "").strip())
    return None


def parse_deals(text: str, kind: str) -> list[dict]:
    """NSE's bulk.csv / block.csv. Columns are found by name, so a reordered file still reads."""
    reader = csv.reader(io.StringIO(text.lstrip("﻿")))
    rows = list(reader)
    if not rows:
        return []
    head = [h.strip().lower() for h in rows[0]]

    def col(*words):
        return next((i for i, h in enumerate(head) if all(w in h for w in words)), None)

    i_date, i_sym, i_name, i_client = col("date"), col("symbol"), col("security"), col("client")
    i_side, i_qty, i_price = col("buy"), col("quantity"), col("price")
    if None in (i_date, i_sym, i_client, i_side, i_qty, i_price):
        return []
    out = []
    for row in rows[1:]:
        if len(row) <= max(i_date, i_sym, i_client, i_side, i_qty, i_price):
            continue
        symbol = row[i_sym].strip().upper()
        qty, price = _number(row[i_qty]), _number(row[i_price])
        side = row[i_side].strip().upper()[:1]
        if not re.fullmatch(r"[A-Z0-9&\-]{1,20}", symbol) or not qty or not price or side not in ("B", "S"):
            continue
        try:
            day = datetime.strptime(row[i_date].strip(), "%d-%b-%Y").strftime("%Y-%m-%d")
        except ValueError:
            continue
        out.append({"date": day, "kind": kind, "symbol": normalize_symbol(f"NSE:{symbol}"), "ticker": symbol,
                    "security": row[i_name].strip() if i_name is not None else "", "client": row[i_client].strip(),
                    "side": "Buy" if side == "B" else "Sell", "quantity": int(qty), "price": round(price, 2),
                    "value_cr": round(qty * price / 1e7, 2)})
    return out


def merge_deals(stored: list[dict], fresh: list[dict], today: str) -> list[dict]:
    """Keep ``KEEP_DEALS_DAYS`` of deals: NSE's file only ever holds the latest day."""
    cutoff = (pd.Timestamp(today) - pd.Timedelta(days=KEEP_DEALS_DAYS)).strftime("%Y-%m-%d")
    seen, out = set(), []
    for deal in fresh + stored:
        k = (deal["date"], deal["kind"], deal["symbol"], deal["client"], deal["side"], deal["quantity"], deal["price"])
        if deal["date"] >= cutoff and k not in seen:
            seen.add(k)
            out.append(deal)
    return sorted(out, key=lambda d: (d["date"], d["value_cr"]), reverse=True)


def volume_spikes(volumes: dict[str, tuple[pd.Series | None, float | None]], prices: dict[str, pd.DataFrame],
                  threshold: float = SPIKE) -> list[dict]:
    """Stocks trading at least ``threshold`` times their 20-day average volume today."""
    out = []
    for symbol, (history, today) in volumes.items():
        if history is None or today is None or len(history) < 20:
            continue
        average = float(history.iloc[-20:].mean())
        if average <= 0 or today < threshold * average:
            continue
        frame = prices.get(symbol)
        change = None
        if frame is not None and len(frame) > 1:
            change = round((frame["Close"].iloc[-1] / frame["Close"].iloc[-2] - 1) * 100, 2)
        last = float(frame["Close"].iloc[-1]) if frame is not None and len(frame) else None
        out.append({"symbol": symbol, "volume": int(today), "average": int(average), "times": round(today / average, 1),
                    "change_1d": change, "price": last,
                    "value_cr": round(today * last / 1e7, 2) if last else None})
    return sorted(out, key=lambda r: -r["times"])


# -- Results Drift -------------------------------------------------------------------------------

def parse_results(answer) -> list[dict]:
    """BSE's results calendar: [{symbol, date}] from rows with short_name and meeting_date ("23 Oct 2023")."""
    if isinstance(answer, dict):
        answer = answer.get("Table") or answer.get("data") or []
    out = []
    for row in answer if isinstance(answer, list) else []:
        if not isinstance(row, dict):
            continue
        name = str(row.get("short_name") or "").strip().upper()
        try:
            day = datetime.strptime(str(row.get("meeting_date") or "").strip(), "%d %b %Y").strftime("%Y-%m-%d")
        except ValueError:
            continue
        if re.fullmatch(r"[A-Z0-9&\-]{1,20}", name):
            out.append({"symbol": normalize_symbol(f"NSE:{name}"), "date": day,
                        "company": " ".join(str(row.get("Long_Name") or "").split())})
    return out


def drift_row(frame: pd.DataFrame, result_day: str, volume: pd.Series | None = None, min_move: float = 4.0) -> dict | None:
    """How a stock reacted to results announced on ``result_day`` and what it has done since.

    Results often come after the close, so the reaction is measured over two sessions: from the close before the
    board meeting to the close of the session after it (or the meeting day's close if that is the latest).
    """
    close = frame["Close"]
    day = pd.Timestamp(result_day)
    before = close[close.index < day]
    after = frame[frame.index >= day]
    if before.empty or after.empty:
        return None
    pre = float(before.iloc[-1])
    react = after.iloc[min(1, len(after) - 1)]
    react_day = after.index[min(1, len(after) - 1)]
    reaction = (float(react["Close"]) / pre - 1) * 100
    last = float(close.iloc[-1])
    since = (last / float(react["Close"]) - 1) * 100
    held_low = float(after["Low"].iloc[:min(2, len(after))].min())
    held_high = float(after["High"].iloc[:min(2, len(after))].max())
    times = None
    if volume is not None and len(volume):
        base = volume[volume.index < day].iloc[-20:]
        on = volume[(volume.index >= day) & (volume.index <= react_day)]
        if len(base) >= 10 and base.mean() > 0 and len(on):
            times = round(float(on.max() / base.mean()), 1)
    if reaction >= min_move and last >= held_low:
        signal = "Drifting up" if since > 0 else "Holding up"
    elif reaction <= -min_move and last <= held_high:
        signal = "Drifting down" if since < 0 else "Holding down"
    else:
        signal = "No edge"
    return {"result_date": result_day, "reaction_date": react_day.strftime("%Y-%m-%d"), "reaction": round(reaction, 2),
            "since": round(since, 2), "sessions": int((close.index > react_day).sum()), "volume_times": times,
            "price": round(last, 2), "signal": signal}


# -- Sector Compass ------------------------------------------------------------------------------

def rotation(series: dict[str, pd.Series], bench: pd.Series, tail: int = 6, length: int = 10,
             weekly: bool = True) -> list[dict]:
    """Relative rotation: strength ratio against ``bench`` and its momentum, both centred on 100, with a tail.

    RS = sector / bench. RS-Ratio = 100 x RS / its ``length``-period average. RS-Momentum = 100 x RS-Ratio / its
    ``length``-period average. Above 100 on both is Leading; strong but slowing is Weakening; below on both is
    Lagging; weak but turning up is Improving.
    """
    def resample(s: pd.Series) -> pd.Series:
        s = s.dropna()
        return s.resample("W-FRI").last().dropna() if weekly else s

    b = resample(bench)
    out = []
    for name, s in series.items():
        s = resample(s)
        both = pd.concat([s, b], axis=1, join="inner").dropna()
        if len(both) < 2 * length + tail:
            continue
        rs = both.iloc[:, 0] / both.iloc[:, 1]
        ratio = 100 * rs / rs.rolling(length).mean()
        mom = 100 * ratio / ratio.rolling(length).mean()
        frame = pd.concat([ratio, mom], axis=1).dropna().iloc[-tail:]
        if frame.empty:
            continue
        x, y = float(frame.iloc[-1, 0]), float(frame.iloc[-1, 1])
        quadrant = ("Leading" if y >= 100 else "Weakening") if x >= 100 else ("Improving" if y >= 100 else "Lagging")
        out.append({"name": name, "ratio": round(x, 2), "momentum": round(y, 2), "quadrant": quadrant,
                    "tail": [{"date": d.strftime("%Y-%m-%d"), "ratio": round(float(r), 2), "momentum": round(float(m), 2)}
                             for d, r, m in frame.itertuples()]})
    return sorted(out, key=lambda r: (["Leading", "Improving", "Weakening", "Lagging"].index(r["quadrant"]), -r["ratio"]))


# -- Stock Radar ---------------------------------------------------------------------------------

def _ret(close: pd.Series, n: int) -> float | None:
    if len(close) <= n:
        return None
    return round((close.iloc[-1] / close.iloc[-1 - n] - 1) * 100, 2)


def radar(stocks: dict[str, pd.DataFrame], nifty: pd.DataFrame | None, volumes: dict, facts, names: dict,
          sectors: dict, rule: dict, flags: dict | None = None) -> list[dict]:
    """One row per stock with a 0-100 score: strength against the Nifty (30), nearness to its 52-week high (20),
    trend above its 50 and 200-day averages (20), one-month momentum (15) and volume against its average (15).
    The strength, momentum and volume parts are ranks within the list, so the score compares stocks with each other."""
    bench = _ret(nifty["Close"], 63) if nifty is not None and len(nifty) > 63 else None
    rows = []
    for symbol, frame in stocks.items():
        close = frame["Close"].dropna()
        if len(close) < 60:
            continue
        last = float(close.iloc[-1])
        year = close.iloc[-252:]
        r3m = _ret(close, 63)
        history, today = volumes.get(symbol, (None, None))
        vol_x = None
        if history is not None and today is not None and len(history) >= 20 and history.iloc[-20:].mean() > 0:
            vol_x = round(float(today / history.iloc[-20:].mean()), 2)
        f = facts(symbol) or {}
        rows.append({"symbol": symbol, "name": names.get(symbol, ""), "sector": ", ".join(sectors.get(symbol, [])[:2]),
                     "price": round(last, 2), "change_1d": _ret(close, 1), "change_1w": _ret(close, 5),
                     "change_1m": _ret(close, 21), "change_3m": r3m, "change_1y": _ret(close, min(len(close) - 1, 250)),
                     "from_high": round((last / year.max() - 1) * 100, 2),
                     "above_50": bool(last > close.iloc[-50:].mean()),
                     "above_200": bool(len(close) >= 200 and last > close.iloc[-200:].mean()),
                     "rs_3m": round(r3m - bench, 2) if r3m is not None and bench is not None else None,
                     "volume_x": vol_x, "trend": trend.classify(frame, rule),
                     "pe": f.get("pe"), "roe": f.get("roe"), "market_cap": f.get("market_cap"),
                     "flags": (flags or {}).get(symbol, [])})

    def ranks(key):
        values = pd.Series({r["symbol"]: r[key] for r in rows if r[key] is not None}, dtype=float)
        return values.rank(pct=True) * 100 if len(values) else values

    rs, mom, vol = ranks("rs_3m"), ranks("change_1m"), ranks("volume_x")
    for r in rows:
        near = _clip(100 + r["from_high"] * 4)                       # at the high = 100, 25% below = 0
        trend_part = 50 * r["above_50"] + 50 * r["above_200"]
        score = (0.30 * rs.get(r["symbol"], 50) + 0.20 * near + 0.20 * trend_part
                 + 0.15 * mom.get(r["symbol"], 50) + 0.15 * vol.get(r["symbol"], 50))
        r["score"] = round(float(score), 1)
    return sorted(rows, key=lambda r: -r["score"])


# -- the service ---------------------------------------------------------------------------------

class MarketTools:
    """Holds the downloaded deals and results calendar and answers the five tools from the desk's candles."""

    def __init__(self, sectors, indices, fundamentals, path: Path | None = None, download=None, get_json=None,
                 fetch=None, now=market.now_ist):
        self.sectors, self.indices, self.fundamentals = sectors, indices, fundamentals
        self._download, self._get_json, self.fetch, self._now = download, get_json, fetch, now
        self.path = path or home() / "market_tools.json"
        self._lock = threading.RLock()
        self._state = {"deals": [], "deals_at": 0, "deals_note": None, "results": [], "results_at": 0,
                       "results_note": None}
        with contextlib.suppress(OSError, json.JSONDecodeError):
            stored = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(stored, dict):
                self._state.update(stored)
        self._pulse_cache: tuple[float, dict] | None = None

    def _save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._state), encoding="utf-8")
        tmp.replace(self.path)

    # -- downloads -------------------------------------------------------------

    def _get(self, url: str) -> str:
        if self._download is not None:
            return self._download(url)
        import httpx

        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                                 "Chrome/131.0 Safari/537.36", "Accept": "text/csv,*/*"}
        response = httpx.get(url, timeout=30, headers=headers, follow_redirects=True)
        response.raise_for_status()
        return response.text

    def refresh_deals(self, force: bool = False) -> None:
        if not force and time.time() - self._state["deals_at"] < DEALS_SECONDS:
            return
        fresh, failed = [], []
        for kind, url in NSE_DEALS.items():
            try:
                fresh += parse_deals(self._get(url), kind)
            except Exception as exc:  # noqa: BLE001 - keep the stored deals
                failed.append(f"{kind}: {str(exc)[:80]}")
        today = self._now().strftime("%Y-%m-%d")
        with self._lock:
            self._state.update(deals=merge_deals(self._state["deals"], fresh, today), deals_at=time.time(),
                               deals_note=("NSE's deal files could not be read (" + "; ".join(failed) + ").")
                               if failed else None)
            self._save()

    def refresh_results(self, force: bool = False) -> None:
        if not force and time.time() - self._state["results_at"] < RESULTS_SECONDS:
            return
        end = self._now().date()
        start = end - timedelta(days=RESULTS_DAYS)
        params = {"fromdate": start.strftime("%Y%m%d"), "todate": end.strftime("%Y%m%d")}
        try:
            if self._get_json is not None:
                answer = self._get_json(BSE_RESULTS, params)
            else:
                from .bselists import http_get_json
                answer = http_get_json(BSE_RESULTS, params)
            rows = parse_results(answer)
            note = None if rows else "BSE's results calendar came back empty."
        except Exception as exc:  # noqa: BLE001 - keep the stored calendar
            rows, note = None, f"BSE's results calendar could not be read: {str(exc)[:100]}"
        with self._lock:
            if rows:
                self._state["results"] = rows
            self._state.update(results_at=time.time(), results_note=note)
            self._save()

    def result_symbols(self, universe: set[str]) -> list[str]:
        """Stocks in ``universe`` that reported in the window, to keep priced."""
        return sorted({r["symbol"] for r in self._state["results"]} & universe)

    def tick(self) -> None:
        self.refresh_deals()
        self.refresh_results()

    # -- shared inputs ---------------------------------------------------------

    def _stocks(self) -> dict[str, pd.DataFrame]:
        out = {}
        for symbol in sorted(set(self.sectors.symbols()) | set(self.sectors.extra)):
            frame = self.sectors.bars(symbol)
            if frame is not None and len(frame) > 1:
                out[symbol] = frame
        return out

    def _nifty(self) -> pd.DataFrame | None:
        frame, _ = self.indices.level("nifty50")
        return frame

    def _vix(self) -> pd.Series | None:
        try:
            return data.history("^INDIAVIX", fetch=self.fetch)["Close"]
        except (data.NoData, KeyError):
            return None

    def _names(self) -> dict[str, str]:
        out = {}
        for index_id in ("nifty50", "niftynext50", "nifty100", "nifty200", "nifty500"):
            for row in self.indices.constituents(index_id):
                out.setdefault(row["symbol"], row["name"])
        return out

    # -- the tools -------------------------------------------------------------

    def pulse(self) -> dict:
        cached = self._pulse_cache
        if cached and time.time() - cached[0] < 120:
            return cached[1]
        stocks = self._stocks()
        out = pulse(self._nifty(), self._vix(), stocks)
        cards = self.indices.cards(self.sectors.rule)["indices"]
        out["sectors"] = [{"id": c["id"], "name": c["name"], "change_1d": c.get("change_1d"), "change_1w": c.get("change_1w"),
                           "change_1m": c.get("change_1m")}
                          for c in cards if c["group"] == "Sectoral" and c.get("value")]
        nifty50 = {r["symbol"] for r in self.indices.constituents("nifty50")}
        out["stocks"] = [{"symbol": s, "change_1d": _ret(f["Close"], 1), "change_1w": _ret(f["Close"], 5),
                          "change_1m": _ret(f["Close"], 21),
                          "market_cap": (self.fundamentals.get(s) or {}).get("market_cap")}
                         for s, f in stocks.items() if s in nifty50]
        out["universe"] = len(stocks)
        out["updated_ist"] = self._now().strftime("%Y-%m-%d %H:%M")
        self._pulse_cache = (time.time(), out)
        return out

    def deals(self, days: int = 5, min_value_cr: float = 0.0) -> dict:
        today = self._now().strftime("%Y-%m-%d")
        cutoff = (pd.Timestamp(today) - pd.Timedelta(days=days)).strftime("%Y-%m-%d")
        rows = [d for d in self._state["deals"] if d["date"] >= cutoff and d["value_cr"] >= min_value_cr]
        stocks = self._stocks()
        volumes = {s: self.sectors.volume(s) for s in stocks}
        return {"deals": rows, "spikes": volume_spikes(volumes, stocks), "note": self._state.get("deals_note"),
                "fetched": self._state["deals_at"], "universe": len(stocks), "threshold": SPIKE}

    def drift(self, min_move: float = 4.0) -> dict:
        out = []
        for row in self._state["results"]:
            frame = self.sectors.bars(row["symbol"])
            if frame is None or len(frame) < 25:
                continue
            volume, _ = self.sectors.volume(row["symbol"])
            found = drift_row(frame, row["date"], volume, min_move)
            if found:
                out.append({"symbol": row["symbol"], "company": row.get("company", ""), **found})
        out.sort(key=lambda r: (r["signal"] == "No edge", -abs(r["reaction"])))
        listed = len(self._state["results"])
        return {"rows": out, "note": self._state.get("results_note"), "fetched": self._state["results_at"],
                "calendar": listed, "priced": len(out), "min_move": min_move}

    def rotation(self, weekly: bool = True) -> dict:
        bench = self._nifty()
        if bench is None or len(bench) < 60:
            return {"rows": [], "note": "Nifty 50 levels have not loaded yet."}
        series = {}
        for entry in self.indices.entries():
            if entry[2] != "Sectoral":
                continue
            frame, _ = self.indices.level(entry[0])
            if frame is not None and len(frame) > 60:
                series[entry[1]] = frame["Close"]
        rows = rotation(series, bench["Close"], weekly=weekly, length=10 if weekly else 20,
                        tail=6 if weekly else 10)
        return {"rows": rows, "note": None if rows else "Not enough sector history yet.", "weekly": weekly}

    def radar(self) -> dict:
        stocks = self._stocks()
        volumes = {s: self.sectors.volume(s) for s in stocks}
        flags: dict[str, list] = {}
        recent = (pd.Timestamp(self._now().date()) - pd.Timedelta(days=5)).strftime("%Y-%m-%d")
        for d in self._state["deals"]:
            if d["date"] >= recent:
                tag = f"{d['kind']} {d['side'].lower()}"
                if tag not in flags.setdefault(d["symbol"], []):
                    flags[d["symbol"]].append(tag)
        for r in self.drift()["rows"]:
            if r["signal"] != "No edge":
                flags.setdefault(r["symbol"], []).append(r["signal"])
        for s, (history, today) in volumes.items():
            if history is not None and today and len(history) >= 20 and today >= SPIKE * history.iloc[-20:].mean() > 0:
                flags.setdefault(s, []).append("Volume spike")
        rows = radar(stocks, self._nifty(), volumes, self.fundamentals.get, self._names(),
                     self.sectors.sector_of(), self.sectors.rule, flags)
        return {"rows": rows, "universe": len(stocks), "updated_ist": self._now().strftime("%Y-%m-%d %H:%M")}


def finite(value):
    """JSON-safe: NaN and infinities become None."""
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    if isinstance(value, dict):
        return {k: finite(v) for k, v in value.items()}
    if isinstance(value, list):
        return [finite(v) for v in value]
    if isinstance(value, np.generic):
        return finite(value.item())
    return value
