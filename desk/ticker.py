"""The index strip along the top of every page: a few index values, live from Kite while the market is open.

One Kite quote call serves the whole strip, at most every ``LIVE_SECONDS`` while the market is open and every
``CLOSED_SECONDS`` while it is closed, however many pages ask. Without Kite the strip shows the index board's last
daily closes (from Yahoo) and says it is offline. Nothing here talks to NSE or BSE directly.
"""

from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Callable

from . import market
from .indices import kite_key

LIVE_SECONDS = 15
CLOSED_SECONDS = 300
FRESH_SECONDS = 120          # quotes older than this, with the market open, read as not live
MAX_INDICES = 30

DEFAULT = ["nifty50", "bsesensex", "niftybank", "niftyfinance", "niftynext50", "niftymidcap100",
           "niftysmallcap100", "indiavix"]

# Indices the strip shows that are not on the Indices board, as (id, label, Kite name).
EXTRA = [("indiavix", "INDIA VIX", "NSE:INDIA VIX")]
EXTRA_BY_ID = {e[0]: e for e in EXTRA}

# The names traders use, where they differ from the board's.
LABELS = {
    "nifty50": "NIFTY 50", "bsesensex": "SENSEX", "bsesensex50": "SENSEX 50", "bsebankex": "BANKEX",
    "niftybank": "BANK NIFTY", "niftyfinance": "FIN NIFTY", "niftynext50": "NIFTY NEXT 50",
    "niftymidcap100": "MIDCAP 100", "niftysmallcap100": "SMALLCAP 100", "niftypsubank": "PSU BANK",
    "niftymidcapselect": "MIDCAP SELECT",
}


class Ticker:
    def __init__(self, indices, chosen: Callable[[], list[str]], now: Callable = market.now_ist):
        self.indices = indices
        self._chosen = chosen
        self._now = now
        self._lock = threading.Lock()
        self._quotes: dict[str, dict] = {}
        self._asked: tuple = ()
        self._at = 0.0                 # when the quotes were fetched
        self._tried = 0.0              # when a fetch was last tried, so a failing Kite is not asked every request

    # -- what can be shown ----------------------------------------------------

    def choices(self) -> list[dict]:
        """Every index the strip can show, for the Settings picker."""
        out = [{"id": e[0], "label": e[1], "group": "Main"} for e in EXTRA]
        for entry in self.indices.entries():
            out.append({"id": entry[0], "label": label_of(entry), "group": entry[2]})
        return out

    def _rows(self) -> list[tuple[str, str, str | None]]:
        """(id, label, Kite name) for each chosen index this desk knows, in the chosen order."""
        rows = []
        for index_id in self._chosen():
            if index_id in EXTRA_BY_ID:
                rows.append(EXTRA_BY_ID[index_id])
                continue
            entry = self.indices.entry(index_id)
            if entry is None:
                continue                                       # a BSE index Kite no longer lists
            name = self.indices.kite_name(index_id) or (kite_key(entry[4][0]) if entry[4] else None)
            rows.append((index_id, label_of(entry), name))
        return rows

    # -- quotes ---------------------------------------------------------------

    def _kite_ready(self) -> bool:
        kite = self.indices.kite
        return bool(kite is not None and kite.connected and "get_quotes" in kite.tools)

    def _refresh(self, names: tuple, is_open: bool) -> None:
        wait = LIVE_SECONDS if is_open else CLOSED_SECONDS
        now = time.time()
        if not names or not self._kite_ready():
            return
        if names == self._asked and now - self._at < wait:
            return
        if now - self._tried < LIVE_SECONDS or not self._lock.acquire(blocking=False):
            return                                             # tried just now, or another request is fetching
        try:
            self._tried = now
            with contextlib.suppress(Exception):
                quotes = self.indices.kite.quotes(list(names))
                if quotes:
                    self._quotes, self._asked, self._at = quotes, names, time.time()
        finally:
            self._lock.release()

    def snapshot(self) -> dict:
        session = market.session_status(self._now())
        is_open = bool(session["open"])
        rows = self._rows()
        self._refresh(tuple(dict.fromkeys(name for _, _, name in rows if name)), is_open)
        quotes, at = self._quotes, self._at
        items, from_kite = [], 0
        for index_id, label, name in rows:
            item = {"id": index_id, "label": label, "value": None, "change": None, "change_pct": None,
                    "chart": f"IDX:{index_id}" if self.indices.kite_name(index_id) else (name or "")}
            quote = quotes.get(name) if name else None
            last = _number(quote.get("last_price")) if quote else None
            previous = _number((quote.get("ohlc") or {}).get("close")) if quote else None
            if last:
                from_kite += 1
                item["kite"] = True
            else:
                item["kite"] = False
                last, previous = self._board_close(index_id)
            if last:
                item["value"] = round(last, 2)
                if previous:
                    item["change"] = round(last - previous, 2)
                    item["change_pct"] = round((last / previous - 1) * 100, 2)
            items.append(item)
        age = time.time() - at if at else None
        if not from_kite:
            state = "offline"
        elif is_open and age is not None and age <= FRESH_SECONDS:
            state = "live"
        elif is_open:
            state = "stale"
        else:
            state = "closed"
        return {"state": state, "items": items, "market_open": is_open,
                "as_of": _ist(at) if at else None,
                "every": LIVE_SECONDS if is_open else CLOSED_SECONDS}

    def _board_close(self, index_id: str) -> tuple[float | None, float | None]:
        """The index board's last two daily closes, when Kite has nothing."""
        if index_id in EXTRA_BY_ID:
            return None, None
        with contextlib.suppress(Exception):
            frame, _ = self.indices.level(index_id)
            if frame is not None and len(frame) > 1:
                close = frame["Close"].dropna()
                if len(close) > 1:
                    return float(close.iloc[-1]), float(close.iloc[-2])
        return None, None


def label_of(entry: tuple) -> str:
    return LABELS.get(entry[0]) or str(entry[1]).upper()


def _number(value) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0 else None


def _ist(epoch: float) -> str:
    from datetime import datetime

    return datetime.fromtimestamp(epoch, market.IST).strftime("%H:%M")


def validate(value) -> list[str]:
    """The ``ticker_indices`` setting: a list of index ids, in the order shown."""
    import re

    if isinstance(value, str):
        value = [v for v in re.split(r"[\s,;]+", value.strip()) if v]
    if not isinstance(value, list) or len(value) > MAX_INDICES:
        raise ValueError(f"Pick up to {MAX_INDICES} indices for the index strip")
    out = []
    for item in value:
        if not isinstance(item, str) or not re.fullmatch(r"[a-z0-9_]{1,40}", item):
            raise ValueError("The index strip takes index ids such as nifty50")
        out.append(item)
    return list(dict.fromkeys(out))
