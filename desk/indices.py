"""The NSE index board: every major Nifty index with its own candles, trend and constituents.

Index levels and candles come from Kite (the official index values) when Kite is
signed in; otherwise from Yahoo for the indices it carries. Constituents come
from NSE's published list for each index, downloaded once a day.

Weights: NSE weights its indices by free-float market cap and publishes the
weights only in monthly factsheets. The desk approximates them by full market
cap where it has the figure (equal weights otherwise), so a stock's share and
contribution here are estimates.
"""

from __future__ import annotations

import contextlib
import csv
import io
import json
import logging
import re
import threading
import time
from pathlib import Path

import pandas as pd

from tradingagents.dataflows.symbols import normalize_symbol

from . import data, market, trend
from .sectors import CARD_CANDLES, candles, change
from .settings import home

logger = logging.getLogger(__name__)

NSE_FILE = "https://nsearchives.nseindia.com/content/indices/{file}.csv"
NIFTY_FILE = "https://www.niftyindices.com/IndexConstituent/{file}.csv"
LIST_SECONDS = 24 * 3600
LEVEL_SECONDS = 6 * 3600

# (id, name, group, constituent file, Kite names to try, Yahoo symbol). File names checked against NSE.
CATALOG = [
    ("nifty50", "Nifty 50", "Broad market", "ind_nifty50list", ["NIFTY 50"], "^NSEI"),
    ("niftynext50", "Nifty Next 50", "Broad market", "ind_niftynext50list", ["NIFTY NEXT 50"], "^NSMIDCP"),
    ("nifty100", "Nifty 100", "Broad market", "ind_nifty100list", ["NIFTY 100"], "^CNX100"),
    ("nifty200", "Nifty 200", "Broad market", "ind_nifty200list", ["NIFTY 200"], "^CNX200"),
    ("nifty500", "Nifty 500", "Broad market", "ind_nifty500list", ["NIFTY 500"], "^CRSLDX"),
    ("niftytotalmarket", "Nifty Total Market", "Broad market", "ind_niftytotalmarket_list", ["NIFTY TOTAL MKT", "NIFTY TOTAL MARKET"], None),
    ("niftymidcap50", "Nifty Midcap 50", "Broad market", "ind_niftymidcap50list", ["NIFTY MIDCAP 50"], "^NSEMDCP50"),
    ("niftymidcap100", "Nifty Midcap 100", "Broad market", "ind_niftymidcap100list", ["NIFTY MIDCAP 100", "NIFTY MID100"], None),
    ("niftymidcap150", "Nifty Midcap 150", "Broad market", "ind_niftymidcap150list", ["NIFTY MIDCAP 150"], None),
    ("niftymidcapselect", "Nifty Midcap Select", "Broad market", "ind_niftymidcapselect_list", ["NIFTY MID SELECT", "NIFTY MIDCAP SELECT"], None),
    ("niftysmallcap50", "Nifty Smallcap 50", "Broad market", "ind_niftysmallcap50list", ["NIFTY SMLCAP 50", "NIFTY SMALLCAP 50"], None),
    ("niftysmallcap100", "Nifty Smallcap 100", "Broad market", "ind_niftysmallcap100list", ["NIFTY SMLCAP 100", "NIFTY SMALLCAP 100"], None),
    ("niftysmallcap250", "Nifty Smallcap 250", "Broad market", "ind_niftysmallcap250list", ["NIFTY SMLCAP 250", "NIFTY SMALLCAP 250"], None),
    ("niftymicrocap250", "Nifty Microcap 250", "Broad market", "ind_niftymicrocap250_list", ["NIFTY MICROCAP250", "NIFTY MICROCAP 250"], None),
    ("niftylargemidcap250", "Nifty LargeMidcap 250", "Broad market", "ind_niftylargemidcap250list", ["NIFTY LARGEMID250", "NIFTY LARGEMIDCAP 250"], None),
    ("niftymidsmallcap400", "Nifty MidSmallcap 400", "Broad market", "ind_niftymidsmallcap400list", ["NIFTY MIDSML 400", "NIFTY MIDSMALLCAP 400"], None),
    ("niftybank", "Nifty Bank", "Sectoral", "ind_niftybanklist", ["NIFTY BANK"], "^NSEBANK"),
    ("niftyprivatebank", "Nifty Private Bank", "Sectoral", "ind_nifty_privatebanklist", ["NIFTY PVT BANK", "NIFTY PRIVATE BANK"], None),
    ("niftypsubank", "Nifty PSU Bank", "Sectoral", "ind_niftypsubanklist", ["NIFTY PSU BANK"], "^CNXPSUBANK"),
    ("niftyfinance", "Nifty Financial Services", "Sectoral", "ind_niftyfinancelist", ["NIFTY FIN SERVICE", "NIFTY FINANCIAL SERVICES"], "NIFTY_FIN_SERVICE.NS"),
    ("niftyfinsrv2550", "Nifty Financial Services 25/50", "Sectoral", "ind_niftyfinancialservices25_50list", ["NIFTY FINSRV25 50"], None),
    ("niftyfinexbank", "Nifty Financial Services ex-Bank", "Sectoral", "ind_niftyFinancialServicesExBank_list", ["NIFTY FINSEREXBNK", "NIFTY FIN SERVICES EX-BANK"], None),
    ("niftyit", "Nifty IT", "Sectoral", "ind_niftyitlist", ["NIFTY IT"], "^CNXIT"),
    ("niftyauto", "Nifty Auto", "Sectoral", "ind_niftyautolist", ["NIFTY AUTO"], "^CNXAUTO"),
    ("niftypharma", "Nifty Pharma", "Sectoral", "ind_niftypharmalist", ["NIFTY PHARMA"], "^CNXPHARMA"),
    ("niftyhealthcare", "Nifty Healthcare", "Sectoral", "ind_niftyhealthcarelist", ["NIFTY HEALTHCARE"], None),
    ("niftyfmcg", "Nifty FMCG", "Sectoral", "ind_niftyfmcglist", ["NIFTY FMCG"], "^CNXFMCG"),
    ("niftymetal", "Nifty Metal", "Sectoral", "ind_niftymetallist", ["NIFTY METAL"], "^CNXMETAL"),
    ("niftyrealty", "Nifty Realty", "Sectoral", "ind_niftyrealtylist", ["NIFTY REALTY"], "^CNXREALTY"),
    ("niftyenergy", "Nifty Energy", "Sectoral", "ind_niftyenergylist", ["NIFTY ENERGY"], "^CNXENERGY"),
    ("niftyoilgas", "Nifty Oil & Gas", "Sectoral", "ind_niftyoilgaslist", ["NIFTY OIL AND GAS", "NIFTY OIL & GAS"], None),
    ("niftymedia", "Nifty Media", "Sectoral", "ind_niftymedialist", ["NIFTY MEDIA"], "^CNXMEDIA"),
    ("niftyconsumerdurables", "Nifty Consumer Durables", "Sectoral", "ind_niftyconsumerdurableslist", ["NIFTY CONSR DURBL", "NIFTY CONSUMER DURABLES"], None),
    ("niftyinfra", "Nifty Infrastructure", "Thematic", "ind_niftyinfralist", ["NIFTY INFRA"], "^CNXINFRA"),
    ("niftycpse", "Nifty CPSE", "Thematic", "ind_niftycpselist", ["NIFTY CPSE"], None),
    ("niftypse", "Nifty PSE", "Thematic", "ind_niftypselist", ["NIFTY PSE"], "^CNXPSE"),
    ("niftycommodities", "Nifty Commodities", "Thematic", "ind_niftycommoditieslist", ["NIFTY COMMODITIES"], "^CNXCMDT"),
    ("niftyconsumption", "Nifty India Consumption", "Thematic", "ind_niftyconsumptionlist", ["NIFTY CONSUMPTION", "NIFTY INDIA CONSUMPTION"], "^CNXCONSUM"),
    ("niftymnc", "Nifty MNC", "Thematic", "ind_niftymnclist", ["NIFTY MNC"], "^CNXMNC"),
    ("niftydefence", "Nifty India Defence", "Thematic", "ind_niftyindiadefence_list", ["NIFTY IND DEFENCE", "NIFTY INDIA DEFENCE"], None),
    ("niftymanufacturing", "Nifty India Manufacturing", "Thematic", "ind_niftyindiamanufacturing_list", ["NIFTY INDIA MFG", "NIFTY IND MFG", "NIFTY INDIA MANUFACTURING"], None),
    ("niftydigital", "Nifty India Digital", "Thematic", "ind_niftyindiadigital_list", ["NIFTY IND DIGITAL", "NIFTY INDIA DIGITAL"], None),
    ("niftycapitalmarkets", "Nifty Capital Markets", "Thematic", "ind_niftycapitalmarkets_list", ["NIFTY CAPITAL MKT", "NIFTY CAPITAL MARKETS"], None),
    ("niftyrural", "Nifty Rural", "Thematic", "ind_niftyrural_list", ["NIFTY RURAL"], None),
    ("niftychemicals", "Nifty Chemicals", "Thematic", "ind_niftychemicals_list", ["NIFTY CHEMICALS"], None),
    ("niftytourism", "Nifty India Tourism", "Thematic", "ind_niftyindiatourism_list", ["NIFTY IND TOURISM", "NIFTY INDIA TOURISM"], None),
    ("niftyrailwayspsu", "Nifty India Railways PSU", "Thematic", "ind_niftyindiarailwayspsu_list", ["NIFTY RAILWAYS PSU", "NIFTY INDIA RAILWAYS PSU"], None),
    ("niftyipo", "Nifty IPO", "Thematic", "ind_niftyipo_list", ["NIFTY IPO"], None),
    ("nifty200momentum30", "Nifty 200 Momentum 30", "Strategy", "ind_nifty200Momentum30_list", ["NIFTY200MOMENTM30", "NIFTY200 MOMENTUM 30"], None),
    ("nifty200quality30", "Nifty 200 Quality 30", "Strategy", "ind_nifty200Quality30_list", ["NIFTY200 QUALTY30", "NIFTY200 QUALITY 30"], None),
    ("nifty100lowvol30", "Nifty 100 Low Volatility 30", "Strategy", "ind_nifty100LowVolatility30list", ["NIFTY100 LOWVOL30", "NIFTY100 LOW VOLATILITY 30"], None),
    ("niftymidcap150momentum50", "Nifty Midcap 150 Momentum 50", "Strategy", "ind_niftyMidcap150Momentum50_list", ["NIFTY M150 MOM 50", "NIFTY MIDCAP150 MOMENTUM 50"], None),
]
BY_ID = {entry[0]: entry for entry in CATALOG}
SYNTHETIC_MAX = 100        # an index this size or smaller can be rebuilt from its stocks when nothing else serves it


def parse_constituents(text: str) -> list[dict]:
    rows = []
    for row in csv.DictReader(io.StringIO(text.lstrip("﻿"))):
        symbol = (row.get("Symbol") or "").strip().upper()
        if symbol and re.fullmatch(r"[A-Z0-9&\-]+", symbol):
            rows.append({"symbol": normalize_symbol(f"NSE:{symbol}"), "name": (row.get("Company Name") or "").strip(),
                         "industry": (row.get("Industry") or "").strip()})
    return rows


class Indices:
    def __init__(self, sectors, fundamentals, kite=None, path: Path | None = None, download=None, fetch=None,
                 now=market.now_ist):
        self.sectors, self.fundamentals, self.kite = sectors, fundamentals, kite
        self._download = download            # (url) -> text; replaces the HTTP call
        self.fetch, self._now = fetch, now
        self.path = path or home() / "indices.json"
        self._lock = threading.RLock()
        self._tick_lock = threading.Lock()
        self._state = {"lists": {}, "listed_at": 0, "kite_names": {}}
        self._levels: dict[str, pd.DataFrame] = {}
        self._level_source: dict[str, str] = {}
        self._level_at = 0.0
        self._live: dict[str, dict] = {}
        self._live_at = 0.0
        with contextlib.suppress(OSError, json.JSONDecodeError):
            stored = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(stored, dict):
                self._state.update(stored)

    def _save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._state), encoding="utf-8")
        tmp.replace(self.path)

    # -- constituents --------------------------------------------------------

    def _get(self, url: str) -> str:
        if self._download is not None:
            return self._download(url)
        import httpx

        response = httpx.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0 (TradeSetu)"}, follow_redirects=True)
        response.raise_for_status()
        return response.text

    def refresh_lists(self, force: bool = False) -> None:
        if not force and time.time() - self._state["listed_at"] < LIST_SECONDS and self._state["lists"]:
            return
        lists = dict(self._state["lists"])
        for index_id, _, _, file, _, _ in CATALOG:
            for template in (NSE_FILE, NIFTY_FILE):
                try:
                    rows = parse_constituents(self._get(template.format(file=file)))
                except Exception:  # noqa: BLE001 - try the other host, then keep yesterday's list
                    continue
                if rows:
                    lists[index_id] = rows
                    break
        with self._lock:
            self._state.update(lists=lists, listed_at=time.time())
            self._save()

    def constituents(self, index_id: str) -> list[dict]:
        return list(self._state["lists"].get(index_id, []))

    def industry_of(self) -> dict[str, str]:
        out = {}
        for rows in self._state["lists"].values():
            for row in rows:
                if row["industry"]:
                    out.setdefault(row["symbol"], row["industry"])
        return out

    # -- index levels --------------------------------------------------------

    def _kite_ready(self) -> bool:
        return bool(self.kite is not None and self.kite.connected and {"get_ltp", "get_historical_data"} <= set(self.kite.tools))

    def resolve_kite_names(self) -> dict[str, str]:
        """Which spelling Kite uses for each index, found by asking for all candidates at once."""
        if not self._kite_ready():
            return self._state["kite_names"]
        candidates = {f"NSE:{name}": index_id for index_id, _, _, _, names, _ in CATALOG for name in names}
        try:
            found = self.kite.ltp(list(candidates))
        except Exception:  # noqa: BLE001 - keep what was resolved before
            return self._state["kite_names"]
        names = {}
        for index_id, _, _, _, options, _ in CATALOG:
            for name in options:
                if f"NSE:{name}" in found:
                    names[index_id] = f"NSE:{name}"
                    break
        with self._lock:
            self._state["kite_names"] = names
            self._save()
        return names

    def kite_name(self, index_id: str) -> str | None:
        return self._state["kite_names"].get(index_id)

    def refresh_levels(self, force: bool = False) -> None:
        if not force and time.time() - self._level_at < LEVEL_SECONDS and self._levels:
            return
        names = self.resolve_kite_names()
        levels, sources = {}, {}
        for index_id, _, _, _, _, yahoo in CATALOG:
            frame = None
            if index_id in names and self._kite_ready():
                with contextlib.suppress(Exception):
                    frame = data.kite_bars(self.kite, names[index_id], "1d", self._now())
                    sources[index_id] = "Kite"
            if (frame is None or frame.empty) and yahoo:
                with contextlib.suppress(data.NoData):
                    frame = data.history(yahoo, fetch=self.fetch)
                    sources[index_id] = "Yahoo"
            if frame is not None and not frame.empty:
                levels[index_id] = frame[["Open", "High", "Low", "Close"]].iloc[-260:]
        with self._lock:
            self._levels, self._level_source, self._level_at = levels, sources, time.time()

    def refresh_live(self) -> None:
        names = self._state["kite_names"]
        if not names or not self._kite_ready() or "get_quotes" not in self.kite.tools:
            return
        with contextlib.suppress(Exception):
            quotes = self.kite.quotes(list(names.values()))
            today = self._now().strftime("%Y-%m-%d")
            with self._lock:
                self._live = {index_id: {"date": today, **quotes[name]} for index_id, name in names.items() if name in quotes}
                self._live_at = time.time()

    def level(self, index_id: str) -> tuple[pd.DataFrame | None, str]:
        """The index's daily candles (today's from the live quote) and where they came from."""
        frame = self._levels.get(index_id)
        source = self._level_source.get(index_id, "")
        if frame is None:
            rows = self.constituents(index_id)
            if rows and len(rows) <= SYNTHETIC_MAX:
                built, _ = self.sectors.index_of({r["symbol"]: 1.0 for r in rows})
                if len(built) > 1:
                    return built, "built from its stocks (equal weight)"
            return None, ""
        live = self._live.get(index_id)
        if live and live.get("last_price"):
            now = self._now()
            day = live.get("ohlc") or {}
            last = float(live["last_price"])
            today = pd.Timestamp(now.date())
            row = pd.DataFrame([[float(day.get("open") or last), max(float(day.get("high") or last), last),
                                 min(float(day.get("low") or last), last), last]], columns=frame.columns, index=[today])
            if frame.index[-1] == today:
                frame = pd.concat([frame.iloc[:-1], row])
            elif market.is_trading_day(now) and now.time() >= market.OPEN:
                frame = pd.concat([frame, row])
        return frame, source

    # -- views ---------------------------------------------------------------

    def cards(self, rule: dict) -> dict:
        out = []
        for index_id, name, group, _, _, _ in CATALOG:
            frame, source = self.level(index_id)
            card = {"id": index_id, "name": name, "group": group, "stocks": len(self.constituents(index_id)),
                    "source": source, "kite": bool(self.kite_name(index_id))}
            if frame is not None and len(frame) > 1:
                close = frame["Close"]
                card.update(trend=trend.classify(frame, rule), value=round(float(close.iloc[-1]), 2),
                            change_1d=change(close, 1), change_1w=change(close, 5), change_1m=change(close, 21),
                            change_3m=change(close, 63), change_1y=change(close, min(len(close) - 1, 250)),
                            candles=candles(frame, CARD_CANDLES))
            else:
                card.update(trend="Loading", candles=[])
            out.append(card)
        return {"indices": out, "kite": self._kite_ready(), "lists_at": self._state["listed_at"],
                "updated_ist": self._now().strftime("%Y-%m-%d %H:%M"), "trend_text": trend.describe(rule)}

    def _previous_close(self, symbol: str) -> float | None:
        """The last daily close before today, for a quote that does not carry one."""
        frame = self.sectors._bars.get(symbol)
        if frame is None or frame.empty:
            return None
        earlier = frame[frame.index < pd.Timestamp(self._now().date())]["Close"].dropna()
        return float(earlier.iloc[-1]) if len(earlier) else None

    def detail(self, index_id: str, rule: dict) -> dict:
        if index_id not in BY_ID:
            raise ValueError("no such index")
        _, name, group, _, _, _ = BY_ID[index_id]
        frame, source = self.level(index_id)
        rows = self.constituents(index_id)
        symbols = [r["symbol"] for r in rows]
        quotes, quote_source = self.sectors.quotes_for(symbols) if symbols else ({}, "")
        caps = {s: (self.fundamentals.get(s) or {}).get("market_cap") for s in symbols}
        known = {s: c for s, c in caps.items() if c}
        by_cap = len(known) == len(symbols) and bool(symbols)
        weights = {s: (known[s] if by_cap else 1.0) for s in symbols}
        total = sum(weights.values()) or 1.0
        stocks = []
        for row in rows:
            symbol, quote = row["symbol"], quotes.get(row["symbol"]) or {}
            last, previous = quote.get("last"), quote.get("prev_close")
            if last and not previous:
                previous = self._previous_close(symbol)
            day = round((last / previous - 1) * 100, 2) if last and previous else None
            share = weights[symbol] / total
            facts = self.fundamentals.get(symbol) or {}
            stocks.append({"symbol": symbol, "name": row["name"], "industry": row["industry"], "price": last,
                           "change_1d": day, "open": quote.get("open"), "high": quote.get("high"), "low": quote.get("low"),
                           "volume": quote.get("volume"), "weight_pct": round(share * 100, 2),
                           "contribution_1d": round(day * share, 3) if day is not None else None,
                           "market_cap": facts.get("market_cap"), "pe": facts.get("pe"), "roe": facts.get("roe")})
        stocks.sort(key=lambda r: (r["contribution_1d"] is None, -(r["contribution_1d"] or 0)))
        out = {"id": index_id, "name": name, "group": group, "source": source, "kite_name": self.kite_name(index_id),
               "stocks": stocks, "weights": "market cap (approximate)" if by_cap else "equal (market caps still loading)",
               "quote_source": quote_source, "candles": [], "trend": "Loading", "trend_text": trend.describe(rule)}
        if frame is not None and len(frame) > 1:
            close = frame["Close"]
            out.update(trend=trend.classify(frame, rule), value=round(float(close.iloc[-1]), 2), candles=candles(frame),
                       change_1d=change(close, 1), change_1w=change(close, 5), change_1m=change(close, 21),
                       change_3m=change(close, 63), change_1y=change(close, min(len(close) - 1, 250)))
        return out

    def kick(self) -> None:
        """Kite was just signed in to: move the board to Kite's values now, in the background, once."""
        if not self._kite_ready() or "Kite" in self._level_source.values() or self._tick_lock.locked():
            return
        def run():
            if self._tick_lock.acquire(blocking=False):
                try:
                    self.refresh_levels(force=True)
                finally:
                    self._tick_lock.release()

        threading.Thread(target=run, name="indices-kick", daemon=True).start()

    def tick(self) -> None:
        if not self._tick_lock.acquire(blocking=False):
            return                                     # one already running
        try:
            self._tick()
        finally:
            self._tick_lock.release()

    def _tick(self) -> None:
        self.refresh_lists()
        # Kite has just been signed in to: switch to its official index values now, not hours later.
        fresh_kite = self._kite_ready() and not self._state["kite_names"]
        self.refresh_levels(force=fresh_kite or (self._kite_ready() and "Kite" not in self._level_source.values()))
        if market.session_status(self._now())["open"] and time.time() - self._live_at > 60:
            self.refresh_live()
        small = [r["symbol"] for index_id in ("nifty50", "niftybank", "niftyit", "niftynext50")
                 for r in self.constituents(index_id)]
        self.fundamentals.queue(sorted(set(small) | set(self.sectors.symbols())))
