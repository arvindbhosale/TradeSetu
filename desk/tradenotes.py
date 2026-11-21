"""The trade journal: why each trade was taken, its plan, and the review after it closed.

A note is written when an order is placed from the ticket (the plan: setup, reason, stoploss, target) or later
from the P&L tab, for any trade. A trade is a position from the fill that opened it until it is flat again
(``pnl.trade_key``). A plan written before its order filled is tied to the first trade of that instrument and
source opened within a few days after it, and keeps that tie.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import secrets
import threading
from datetime import datetime, timedelta
from pathlib import Path

from . import market
from .settings import home

# Mistakes a review can tick. Short, plain names: the analytics group trades by them.
MISTAKES = (
    "No stoploss",
    "Moved the stoploss",
    "Entered late (chased)",
    "Exited early",
    "Held past the stoploss",
    "Too big",
    "Against the trend",
    "Revenge trade",
    "Did not follow the plan",
)
# Setups offered before the user has typed their own; any text is allowed.
SUGGESTED_SETUPS = (
    "Breakout", "Pullback in uptrend", "Breakdown (short)", "Range: buy support", "Gap and go", "Opening range breakout",
    "Moving average cross", "Results drift", "Mean reversion (RSI)", "Option buy on trend", "Option selling (range)",
)
LINK_DAYS = 4                  # a plan ties to a trade opened up to this many days after it (an AMO, a slow fill)
MAX_NOTES = 5000
_TEXT = {"setup": 40, "reason": 600, "lesson": 600, "symbol": 40, "source": 80}


def _when(text: str) -> datetime | None:
    try:
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None


class TradeNotes:
    def __init__(self, path: Path | None = None, now=market.now_ist):
        self.path = path or home() / "trade_notes.json"
        self._now = now
        self._lock = threading.RLock()

    # -- storage -------------------------------------------------------------

    def _read(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write(self, data: dict) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        with contextlib.suppress(OSError):
            os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    def all(self) -> list[dict]:
        with self._lock:
            return sorted(self._read().values(), key=lambda n: n.get("created", ""))

    # -- edits ---------------------------------------------------------------

    @staticmethod
    def _clean(raw: dict, partial: bool) -> dict:
        out: dict = {}
        for key, limit in _TEXT.items():
            if key in raw:
                value = raw[key]
                if value is None:
                    value = ""
                if not isinstance(value, str):
                    raise ValueError(f"{key} must be text")
                value = " ".join(value.split()) if key in ("setup", "symbol", "source") else value.strip()
                if len(value) > limit:
                    raise ValueError(f"{key} is longer than {limit} characters")
                out[key] = value
        for key in ("stop", "target"):
            if key in raw:
                value = raw[key]
                if value in (None, "", 0):
                    out[key] = None
                elif isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                    raise ValueError(f"{key} must be a price above zero")
                else:
                    out[key] = round(float(value), 2)
        if "followed_plan" in raw:
            if raw["followed_plan"] not in ("yes", "no", "", None):
                raise ValueError("followed_plan must be yes, no or empty")
            out["followed_plan"] = raw["followed_plan"] or ""
        if "mistakes" in raw:
            value = raw["mistakes"] or []
            if not isinstance(value, list) or not set(value) <= set(MISTAKES):
                raise ValueError("mistakes must be a list from: " + ", ".join(MISTAKES))
            out["mistakes"] = [m for m in MISTAKES if m in value]
        if not partial:
            mode = raw.get("mode")
            if mode not in ("paper", "live"):
                raise ValueError("mode must be paper or live")
            out["mode"] = mode
            exchange = str(raw.get("exchange", "")).upper()
            if exchange not in ("NSE", "BSE", "NFO", "BFO", "MCX"):
                raise ValueError("exchange must be NSE, BSE, NFO, BFO or MCX")
            out["exchange"] = exchange
            out["symbol"] = out.get("symbol", "").upper()
            if not out["symbol"]:
                raise ValueError("symbol is needed")
            out["source"] = out.get("source") or "manual"
            key = raw.get("key") or ""
            if not isinstance(key, str) or len(key) > 200:
                raise ValueError("key is not a trade key")
            if key and not key.startswith(f"{mode}|"):
                raise ValueError("the trade belongs to the other account")
            out["key"] = key
        return out

    def add(self, raw: dict) -> dict:
        note = self._clean(raw, partial=False)
        with self._lock:
            data = self._read()
            if len(data) >= MAX_NOTES:
                raise ValueError(f"The journal holds at most {MAX_NOTES} notes. Delete old ones first.")
            if note["key"] and any(n.get("key") == note["key"] for n in data.values()):
                raise ValueError("This trade already has a note. Edit that one.")
            note = {"id": secrets.token_hex(6), "created": self._now().strftime("%Y-%m-%d %H:%M:%S"), "setup": "",
                    "reason": "", "stop": None, "target": None, "followed_plan": "", "mistakes": [], "lesson": "",
                    **note}
            data[note["id"]] = note
            self._write(data)
            return dict(note)

    def update(self, note_id: str, raw: dict) -> dict:
        changes = self._clean(raw, partial=True)
        for fixed in ("mode", "exchange", "symbol", "source"):
            changes.pop(fixed, None)          # what the note is about does not change
        with self._lock:
            data = self._read()
            if note_id not in data:
                raise KeyError(note_id)
            data[note_id].update(changes, updated=self._now().strftime("%Y-%m-%d %H:%M:%S"))
            self._write(data)
            return dict(data[note_id])

    def delete(self, note_id: str) -> None:
        with self._lock:
            data = self._read()
            if data.pop(note_id, None) is None:
                raise KeyError(note_id)
            self._write(data)

    # -- tying notes to trades ---------------------------------------------------

    def attach(self, trades: list[dict]) -> dict[str, dict]:
        """Notes by trade key for ``trades`` (each with key, mode, source, exchange, symbol, opened_at). A plan with
        no trade yet is tied to the first matching trade opened from a minute before it to LINK_DAYS after, and the
        tie is saved so it never moves to another trade."""
        with self._lock:
            data = self._read()
            by_key = {n["key"]: n for n in data.values() if n.get("key")}
            free = sorted((n for n in data.values() if not n.get("key")), key=lambda n: n.get("created", ""))
            changed = False
            for note in free:
                created = _when(note.get("created"))
                if created is None:
                    continue
                for trade in sorted(trades, key=lambda t: t["opened_at"]):
                    if trade["key"] in by_key:
                        continue
                    if (trade["mode"], trade["source"], trade["exchange"], trade["symbol"]) != (
                            note["mode"], note["source"], note["exchange"], note["symbol"]):
                        continue
                    opened = _when(trade["opened_at"])
                    if opened and created - timedelta(minutes=1) <= opened <= created + timedelta(days=LINK_DAYS):
                        note["key"] = trade["key"]
                        by_key[trade["key"]] = note
                        changed = True
                        break
            if changed:
                self._write(data)
            return {k: dict(v) for k, v in by_key.items()}

    def pending(self, mode: str) -> list[dict]:
        """Plans not tied to a trade yet (the order has not filled, or never did)."""
        return [n for n in self.all() if n["mode"] == mode and not n.get("key")]

    def setups(self) -> list[str]:
        used = []
        for note in self.all():
            if note.get("setup") and note["setup"] not in used:
                used.append(note["setup"])
        return used + [s for s in SUGGESTED_SETUPS if s not in used]
