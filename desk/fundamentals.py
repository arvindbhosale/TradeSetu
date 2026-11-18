"""Company fundamentals: market cap, valuation, returns on capital, growth, dividends.

Kite's API carries no fundamentals (the numbers on Kite's own screens come from
a separate provider and are not offered to API users), so these come from
Yahoo Finance's company summary. They are refreshed every few days, cached on
disk, and fetched slowly in the background so the feed is not hammered.

Yahoo's figures are trailing-twelve-month or latest-reported values; a field
the feed does not carry for a company is left empty rather than guessed.
"""

from __future__ import annotations

import contextlib
import json
import logging
import threading
import time
from pathlib import Path

from tradingagents.dataflows.symbols import normalize_symbol

from .settings import home

logger = logging.getLogger(__name__)

MAX_AGE = 3 * 86400
FIELDS = {
    # ours: (yahoo key, scale)
    "market_cap": ("marketCap", 1), "pe": ("trailingPE", 1), "forward_pe": ("forwardPE", 1),
    "pb": ("priceToBook", 1), "eps": ("trailingEps", 1), "book_value": ("bookValue", 1),
    "roe": ("returnOnEquity", 100), "roa": ("returnOnAssets", 100), "debt_to_equity": ("debtToEquity", 0.01),
    "profit_margin": ("profitMargins", 100), "operating_margin": ("operatingMargins", 100),
    "revenue_growth": ("revenueGrowth", 100), "earnings_growth": ("earningsGrowth", 100),
    "dividend_yield": ("dividendYield", 1), "beta": ("beta", 1), "revenue": ("totalRevenue", 1),
    "promoter_holding": ("heldPercentInsiders", 100), "institution_holding": ("heldPercentInstitutions", 100),
    "sector": ("sector", None), "industry": ("industry", None), "name": ("longName", None),
}
LABELS = {
    "market_cap": "Market cap", "pe": "P/E (TTM)", "forward_pe": "Forward P/E", "pb": "Price / book",
    "eps": "EPS (TTM) ₹", "book_value": "Book value / share ₹", "roe": "ROE %", "roa": "ROA %",
    "debt_to_equity": "Debt / equity", "profit_margin": "Net margin %", "operating_margin": "Operating margin %",
    "revenue_growth": "Revenue growth % (y/y)", "earnings_growth": "Earnings growth % (y/y)",
    "dividend_yield": "Dividend yield %", "beta": "Beta", "revenue": "Revenue (TTM)",
    "promoter_holding": "Insider / promoter holding %", "institution_holding": "Institutional holding %",
}


def _from_info(info: dict) -> dict:
    out = {}
    for ours, (key, scale) in FIELDS.items():
        value = info.get(key)
        if value is None or value == "" or (isinstance(value, float) and value != value):
            continue
        if scale is None:
            out[ours] = str(value)
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            out[ours] = round(float(value) * scale, 2 if ours not in ("market_cap", "revenue") else 0)
    return out


class Fundamentals:
    def __init__(self, path: Path | None = None, fetch=None):
        self.path = path or home() / "fundamentals.json"
        self._fetch = fetch                      # (symbol) -> info dict; replaces the Yahoo call
        self._lock = threading.Lock()
        self._data: dict[str, dict] = {}
        self._queue: list[str] = []
        self._thread: threading.Thread | None = None
        with contextlib.suppress(OSError, json.JSONDecodeError):
            stored = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(stored, dict):
                self._data = stored

    def _save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data), encoding="utf-8")
        tmp.replace(self.path)

    def get(self, symbol: str) -> dict | None:
        entry = self._data.get(normalize_symbol(symbol))
        return entry["values"] if entry else None

    def fresh(self, symbol: str) -> bool:
        entry = self._data.get(normalize_symbol(symbol))
        return bool(entry) and time.time() - entry["at"] < MAX_AGE

    def load(self, symbol: str) -> dict:
        """Fetch one company's figures now (and cache them)."""
        canonical = normalize_symbol(symbol)
        if self.fresh(canonical):
            return self.get(canonical)
        try:
            if self._fetch is not None:
                info = self._fetch(canonical)
            else:
                import yfinance as yf

                from tradingagents.dataflows.vendors.yahoo.common import yf_retry

                info = yf_retry(lambda: yf.Ticker(canonical).info)
        except Exception as exc:  # noqa: BLE001 - no figures is a state the screen can show
            logger.info("fundamentals for %s unavailable: %s", canonical, exc)
            info = None
        values = _from_info(info or {})
        with self._lock:
            if values or canonical not in self._data:
                self._data[canonical] = {"at": time.time(), "values": values}
                self._save()
        return values

    def queue(self, symbols) -> None:
        """Fetch these in the background, one at a time, skipping any still fresh."""
        with self._lock:
            known = set(self._queue)
            self._queue.extend(s for s in symbols if s not in known and not self.fresh(s))
        if self._thread and self._thread.is_alive():
            return

        def run():
            while True:
                with self._lock:
                    if not self._queue:
                        return
                    symbol = self._queue.pop(0)
                self.load(symbol)
                time.sleep(0.4 if self._fetch is None else 0)

        self._thread = threading.Thread(target=run, name="desk-fundamentals", daemon=True)
        self._thread.start()

    def pending(self) -> int:
        return len(self._queue)
