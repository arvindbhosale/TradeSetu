"""Replay charts: past candles shown one at a time for practice, and the practice trades made on them.

A replay hides every candle after a chosen moment and reveals them one by one; indicators are worked out in the
browser on the shown candles only, so nothing after the cut can leak into them. Practice trades are kept here,
apart from the paper book, the live account, the journal and algo statistics, and they never become orders.

Candles come from the same places as the live chart: the broker (Kite keeps intraday history from Feb 2015),
Binance's public history for crypto, or an imported file. Each replay loads a window around the start: a warm-up
before it (so a 200-period average is ready on the first candle) and a stretch after it; more is loaded when the
replay reaches the end.
"""
from __future__ import annotations

import json
import math
import random
import re
import secrets
import threading
import time
from pathlib import Path

import pandas as pd

from .marketdata import TIMEFRAMES

# Candles before the cut (indicator warm-up) and after it (the replay itself), per load.
WARM_BARS = 300
AHEAD_BARS = 1500
# Calendar days of broker history that hold about that many candles (NSE: 375 one-minute candles a day,
# about 250 trading days a year).
WARM_DAYS = {"1m": 4, "5m": 10, "15m": 30, "1h": 90, "4h": 200, "1d": 450, "1w": 2600}
AHEAD_DAYS = {"1m": 7, "5m": 30, "15m": 90, "1h": 300, "4h": 750, "1d": 2200, "1w": 3650}
# Earliest start the desk offers. Kite: intraday from 2 Feb 2015 (a month of warm-up after it); day candles
# from 2008. Binance spot history starts in Aug 2017 for the oldest pairs.
FLOOR = {"india_intraday": "2015-03-02", "india_daily": "2008-01-01", "crypto": "2018-01-01"}
SESSION_OPEN = (9, 15)

MAX_SESSIONS = 300
MAX_TRADES = 500
REASONS = ("Stoploss", "Target", "Exit", "Square-off", "End of replay")
_ID = re.compile(r"[0-9a-f]{12}")


def intraday(tf: str) -> bool:
    return TIMEFRAMES[tf]["seconds"] < 86400


def parse_start(raw: str, tf: str, venue: str) -> pd.Timestamp:
    """``YYYY-MM-DD`` or ``YYYY-MM-DDTHH:MM`` on the India clock. A date alone starts at the session's open for
    NSE/BSE intraday candles, at 05:30 (00:00 UTC, crypto's day) for crypto."""
    text = str(raw or "").strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2})?", text):
        raise ValueError("Pick the replay's start date (and time, if you want one).")
    try:
        stamp = pd.Timestamp(text.replace(" ", "T"))
    except ValueError as exc:
        raise ValueError(f"{text} is not a date.") from exc
    if len(text) == 10 and intraday(tf):
        stamp = stamp + (pd.Timedelta(hours=5, minutes=30) if venue == "crypto"
                         else pd.Timedelta(hours=SESSION_OPEN[0], minutes=SESSION_OPEN[1]))
    return stamp


def floor_for(venue: str, tf: str) -> pd.Timestamp:
    if venue == "crypto":
        return pd.Timestamp(FLOOR["crypto"])
    return pd.Timestamp(FLOOR["india_intraday" if intraday(tf) else "india_daily"])


def check_start(stamp: pd.Timestamp, venue: str, tf: str, now: pd.Timestamp) -> None:
    if venue != "imported" and stamp < floor_for(venue, tf):
        where = "Binance" if venue == "crypto" else "Broker"
        raise ValueError(f"{where} history for {TIMEFRAMES[tf]['label']} candles starts {floor_for(venue, tf):%d %b %Y}"
                         f" here. Pick a later start.")
    if stamp >= now:
        raise ValueError("Pick a start in the past: a replay shows candles that have already happened.")


def random_start(venue: str, tf: str, today: pd.Timestamp, rng: random.Random,
                 first: pd.Timestamp | None = None, last: pd.Timestamp | None = None) -> pd.Timestamp:
    """A random weekday start with a replay's worth of candles after it (blind mode). ``first``/``last`` bound
    an imported file's dates."""
    lo = first if first is not None else floor_for(venue, tf)
    hi = (last if last is not None else today) - pd.Timedelta(days=max(3, AHEAD_DAYS[tf] // 3))
    if hi <= lo:
        hi = (last if last is not None else today) - pd.Timedelta(days=1)
    if hi <= lo:
        raise ValueError("There is not enough history for a random replay.")
    for _ in range(20):
        day = (lo + pd.Timedelta(days=rng.randrange(max(1, (hi - lo).days)))).normalize()
        if day.weekday() < 5 or venue == "crypto":
            break
    return parse_start(day.strftime("%Y-%m-%d"), tf, venue)


def epoch(stamp: pd.Timestamp) -> int:
    """An India-clock time as the chart's bar time (seconds, India wall clock read as UTC)."""
    return int(pd.Timestamp(stamp).value // 1_000_000_000)


def window(rows: list[dict], cut: int, seconds: int, warm: int = WARM_BARS,
           ahead: int = AHEAD_BARS) -> tuple[list[dict], int]:
    """``warm`` candles finished by ``cut`` and ``ahead`` from there, and how many of the returned candles are the
    finished ones (shown when the replay starts). A candle still forming at the cut (a 15-minute candle that
    started at 10:15 when the cut is 10:20) is hidden: its high, low and close are in the future."""
    done = [r for r in rows if r["time"] + seconds <= cut]
    before = done[-warm:] if warm else []
    after = rows[len(done):][:ahead]
    return before + after, len(before)


# -- practice results --------------------------------------------------------------------------------------
def _num(raw, name: str, positive: bool = True) -> float:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(raw):
        raise ValueError(f"{name} must be a number")
    if positive and raw <= 0:
        raise ValueError(f"{name} must be above zero")
    return float(raw)


def _clean_trade(raw) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("each practice trade must be an object")
    side = raw.get("side")
    if side not in ("long", "short"):
        raise ValueError("a practice trade's side is long or short")
    reason = raw.get("reason")
    if reason not in REASONS:
        raise ValueError(f"a practice trade's exit reason is one of {', '.join(REASONS)}")
    product = raw.get("product") or "intraday"
    if product not in ("intraday", "delivery"):
        raise ValueError("product is intraday or delivery")
    return {"side": side, "qty": _num(raw.get("qty"), "Quantity"), "entry": _num(raw.get("entry"), "Entry price"),
            "exit": _num(raw.get("exit"), "Exit price"), "entry_time": int(_num(raw.get("entry_time"), "Entry time")),
            "exit_time": int(_num(raw.get("exit_time"), "Exit time")), "reason": reason, "product": product}


def gross(trade: dict) -> float:
    move = trade["exit"] - trade["entry"]
    return (move if trade["side"] == "long" else -move) * trade["qty"]


def summary(trades: list[dict]) -> dict:
    """Totals of practice trades: count, wins, win rate, P&L before and after charges, worst drawdown."""
    nets = [t["net"] for t in trades]
    wins = [n for n in nets if n > 0]
    losses = [n for n in nets if n <= 0]
    peak = run = worst = 0.0
    for n in nets:
        run += n
        peak = max(peak, run)
        worst = min(worst, run - peak)
    count = len(trades)
    return {"trades": count, "wins": len(wins), "win_rate": round(100 * len(wins) / count, 1) if count else None,
            "gross": round(sum(t["gross"] for t in trades), 2), "charges": round(sum(t["charges"] for t in trades), 2),
            "net": round(sum(nets), 2), "max_drawdown": round(worst, 2),
            "avg_win": round(sum(wins) / len(wins), 2) if wins else None,
            "avg_loss": round(sum(losses) / len(losses), 2) if losses else None}


class ReplayStore:
    """Practice sessions in one JSON file. ``charges(session, trade)`` gives a trade's round-trip charges."""

    def __init__(self, path: Path, charges=None):
        self.path = Path(path)
        self.charges = charges or (lambda session, trade: 0.0)
        self._lock = threading.Lock()

    def _load(self) -> list[dict]:
        try:
            items = json.loads(self.path.read_text(encoding="utf-8"))
            return items if isinstance(items, list) else []
        except (OSError, ValueError):
            return []

    def _save(self, items: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(items), encoding="utf-8")
        tmp.replace(self.path)

    def save(self, raw) -> dict:
        """Adds or replaces a session (by ``id``); charges and totals are worked out here, not trusted."""
        if not isinstance(raw, dict):
            raise ValueError("the practice session must be an object")
        sid = str(raw.get("id") or "")
        if sid and not _ID.fullmatch(sid):
            raise ValueError("bad session id")
        venue = raw.get("venue")
        if venue not in ("india", "crypto", "imported"):
            raise ValueError("venue must be india, crypto or imported")
        tf = raw.get("tf")
        if tf not in TIMEFRAMES:
            raise ValueError("unknown candle size")
        symbol = str(raw.get("symbol") or "").strip().upper()
        if not symbol or len(symbol) > 40:
            raise ValueError("A symbol needs 1 to 40 characters.")
        trades = raw.get("trades") or []
        if not isinstance(trades, list) or len(trades) > MAX_TRADES:
            raise ValueError(f"A practice session holds {MAX_TRADES} trades at most.")
        session = {"id": sid or secrets.token_hex(6), "venue": venue, "symbol": symbol,
                   "name": str(raw.get("name") or symbol)[:60], "tf": tf, "start": int(_num(raw.get("start"), "Start")),
                   "blind": bool(raw.get("blind")), "ended": bool(raw.get("ended")), "updated": int(time.time())}
        clean = []
        for t in trades:
            t = _clean_trade(t)
            t["gross"] = round(gross(t), 2)
            try:
                t["charges"] = round(max(0.0, float(self.charges(session, t))), 2)
            except Exception:  # noqa: BLE001 - an unknown instrument has no charges rather than no result
                t["charges"] = 0.0
            t["net"] = round(t["gross"] - t["charges"], 2)
            clean.append(t)
        session["trades"] = clean
        session["summary"] = summary(clean)
        with self._lock:
            loaded = self._load()
            old = next((s for s in loaded if s.get("id") == session["id"]), None)
            items = [s for s in loaded if s.get("id") != session["id"]]
            session["created"] = old.get("created", session["updated"]) if old else session["updated"]
            items.append(session)
            items = items[-MAX_SESSIONS:]
            self._save(items)
        return session

    def list(self) -> dict:
        """Sessions newest first (a blind session's stock stays hidden until it is ended), and the score over all."""
        with self._lock:
            items = self._load()
        out = []
        for s in sorted(items, key=lambda x: x.get("updated", 0), reverse=True):
            hide = s.get("blind") and not s.get("ended")
            out.append({k: v for k, v in s.items() if k != "trades"}
                       | ({"symbol": "Hidden", "name": "Hidden until you end it"} if hide else {}))
        every = [t for s in items for t in s.get("trades", [])]
        return {"sessions": out, "score": summary(every)}

    def get(self, sid: str) -> dict:
        with self._lock:
            found = next((s for s in self._load() if s.get("id") == sid), None)
        if not found:
            raise KeyError(sid)
        return found

    def delete(self, sid: str) -> bool:
        with self._lock:
            items = self._load()
            kept = [s for s in items if s.get("id") != sid]
            if len(kept) == len(items):
                return False
            self._save(kept)
        return True
