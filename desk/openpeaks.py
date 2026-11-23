"""The best and worst open profit of each trade, day by day.

While a position is open the desk samples its open (unrealised) P&L about once a minute and keeps, for each
trading day, the highest and the lowest value it saw and when. A trade that peaked at +₹4,000 and was closed
at +₹500 says something about the exits that the booked result alone does not.

Kept per trade (``pnl.trade_key``) and per source (an algo: the sum of all its open legs, which is what an
option strategy's peak means). Only seen while the desk runs: a peak reached while it was off is not recorded.
Figures are before the charges of closing, like the open P&L everywhere else on the desk.
"""

from __future__ import annotations

import contextlib
import json
import threading
from datetime import time as clock_time
from pathlib import Path

from . import market

SAMPLE_SECONDS = 60
FROM, UNTIL = clock_time(9, 15), clock_time(23, 30)       # NSE/BSE open to the MCX close
KEEP_DAYS = 400


class OpenPeaks:
    def __init__(self, desk, path: Path, now=market.now_ist):
        self.desk, self.path, self._now = desk, path, now
        self._lock = threading.RLock()
        self._last = None
        self._data: dict = {"trades": {}, "sources": {}}
        with contextlib.suppress(OSError, json.JSONDecodeError, AttributeError):
            stored = json.loads(path.read_text(encoding="utf-8"))
            self._data = {"trades": dict(stored.get("trades") or {}), "sources": dict(stored.get("sources") or {})}

    def _save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data), encoding="utf-8")
        tmp.replace(self.path)

    @staticmethod
    def _mark(days: dict, day: str, at: str, value: float) -> bool:
        value = round(float(value), 2)
        row = days.get(day)
        if row is None:
            days[day] = {"high": value, "high_at": at, "low": value, "low_at": at}
            if len(days) > KEEP_DAYS:
                for old in sorted(days)[:-KEEP_DAYS]:
                    del days[old]
            return True
        changed = False
        if value > row["high"]:
            row["high"], row["high_at"], changed = value, at, True
        if value < row["low"]:
            row["low"], row["low_at"], changed = value, at, True
        return changed

    def record(self, mode: str, open_rows: list[dict]) -> bool:
        """Take one sample of an account's open positions (``Ledger.report(...)["open"]``)."""
        now = self._now()
        day, at = now.strftime("%Y-%m-%d"), now.strftime("%H:%M:%S")
        sums: dict[str, float] = {}
        unpriced: set[str] = set()
        changed = False
        with self._lock:
            for row in open_rows:
                source = row.get("source") or "manual"
                if row.get("unrealized") is None:
                    unpriced.add(source)             # one leg without a price: the algo's total would be wrong
                    continue
                changed |= self._mark(self._data["trades"].setdefault(row["key"], {}), day, at, row["unrealized"])
                sums[source] = sums.get(source, 0.0) + row["unrealized"]
            for source, total in sums.items():
                if source in unpriced or source == "manual":    # manual trades are unrelated: no combined figure
                    continue
                changed |= self._mark(self._data["sources"].setdefault(f"{mode}|{source}", {}), day, at, total)
            if changed:
                self._save()
        return changed

    def tick(self) -> None:
        """Sample both accounts, at most once a minute, on trading days between 09:15 and 23:30 IST."""
        now = self._now()
        if not market.is_trading_day(now) or not FROM <= now.time() <= UNTIL:
            return
        if self._last and (now - self._last).total_seconds() < SAMPLE_SECONDS - 1:
            return
        self._last = now
        for mode in ("paper", "live"):
            rows = self.desk.ledger.report(mode)["open"]          # no prices: just which positions are open
            if not rows:
                continue
            prices = self._prices([f"{r['exchange']}:{r['symbol']}" for r in rows])
            self.record(mode, self.desk.ledger.report(mode, prices.get)["open"])

    def _prices(self, instruments: list[str]) -> dict[str, float]:
        """Last prices, in one Kite call when it is connected; the desk's usual lookup for any it did not give."""
        out: dict[str, float] = {}
        kite = self.desk.kite
        if kite.connected and "get_ltp" in kite.tools:
            with contextlib.suppress(Exception):
                out.update({k: v for k, v in kite.ltp(sorted(set(instruments))).items() if v})
        for instrument in set(instruments) - set(out):
            price = self.desk.last_price(instrument)[0]
            if price:
                out[instrument] = price
        return out

    @staticmethod
    def _rows(days: dict) -> list[dict]:
        return [{"day": day, **days[day]} for day in sorted(days, reverse=True)]

    def trade(self, key: str) -> list[dict]:
        """One trade's days, newest first: ``day``, ``high``, ``high_at``, ``low``, ``low_at``."""
        with self._lock:
            return self._rows(self._data["trades"].get(key) or {})

    def source(self, mode: str, source: str) -> list[dict]:
        """An algo's days (all its open legs added up), newest first."""
        with self._lock:
            return self._rows(self._data["sources"].get(f"{mode}|{source}") or {})

    def attach(self, trades: list[dict]) -> list[dict]:
        """Add ``daily`` (each day's high and low), ``best_open`` and ``worst_open`` to journal trades."""
        for trade in trades:
            daily = self.trade(trade["key"])
            trade["daily"] = daily
            trade["best_open"] = max((d["high"] for d in daily), default=None)
            trade["worst_open"] = min((d["low"] for d in daily), default=None)
        return trades
