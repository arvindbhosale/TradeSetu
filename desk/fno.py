"""Futures and options: which indices and stocks trade in F&O, their lot sizes and expiry dates.

Everything comes from Kite's instrument list, which the exchange refreshes
every morning, so a contract that has expired drops out and the next one
takes its place without anything being edited here. No date is computed by
rule: expiry days have moved more than once and shift for holidays.
"""

from __future__ import annotations

import contextlib
import json
import logging
import threading
from pathlib import Path

import pandas as pd

from . import market, mcx
from .kite import KiteError
from .settings import home

logger = logging.getLogger(__name__)

# Index underlyings with listed options, and the exchange segment they trade in.
INDICES = {"NIFTY": "NFO", "BANKNIFTY": "NFO", "FINNIFTY": "NFO", "MIDCPNIFTY": "NFO", "NIFTYNXT50": "NFO",
           "SENSEX": "BFO", "BANKEX": "BFO"}


def _date(value) -> str | None:
    if not value:
        return None
    with contextlib.suppress(ValueError, TypeError):
        stamp = pd.to_datetime(str(value))
        return stamp.strftime("%Y-%m-%d")
    return None


class Derivatives:
    def __init__(self, kite, path: Path | None = None, now=market.now_ist):
        self.kite, self._now = kite, now
        self.path = path or home() / "fno.json"
        self._lock = threading.RLock()
        self._data: dict = {"fetched": None, "indices": [], "stocks": [], "contracts": {}}
        self._by_symbol: dict[tuple[str, str], dict] = {}
        with contextlib.suppress(OSError, json.JSONDecodeError):
            stored = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(stored, dict) and "stocks" in stored:
                self._data = {"contracts": {}, **stored}
        self._index_contracts()

    def _rows(self, query: str, filter_on: str) -> list:
        answer = self.kite.call("search_instruments", {"query": query, "filter_on": filter_on}, timeout=90.0)
        if isinstance(answer, dict):
            answer = answer.get("data") or []
        return [row for row in answer if isinstance(row, dict)] if isinstance(answer, list) else []

    def refresh(self) -> dict:
        """Rebuild the list from Kite. Raises KiteError when Kite cannot be asked."""
        if not self.kite.connected or "search_instruments" not in self.kite.tools:
            raise KiteError("Connect Kite on the Trade tab: the F&O list comes from Kite's instrument file.")
        today = self._now().strftime("%Y-%m-%d")
        futures: dict[tuple[str, str], dict] = {}
        for row in self._rows("FUT", "tradingsymbol"):
            if row.get("exchange") not in ("NFO", "BFO") or row.get("instrument_type") != "FUT":
                continue
            expiry, name = _date(row.get("expiry_date")), str(row.get("name") or "").strip().upper()
            if not name or not expiry or expiry < today:
                continue
            entry = futures.setdefault((row["exchange"], name), {"name": name, "exchange": row["exchange"],
                                                                 "lot_size": int(row.get("lot_size") or 0), "monthly": set(),
                                                                 "contracts": []})
            entry["monthly"].add(expiry)
            if row.get("tradingsymbol"):
                entry["contracts"].append({"exchange": row["exchange"], "tradingsymbol": str(row["tradingsymbol"]).upper(),
                                           "underlying": name, "type": "FUT", "expiry": expiry,
                                           "lot_size": int(row.get("lot_size") or 0) or 1,
                                           "tick_size": float(row.get("tick_size") or 0) or 0.05})
        if not futures:
            raise KiteError("Kite returned no futures contracts. Its instrument list may still be loading; try again shortly.")

        indices, stocks = [], []
        for (exchange, name), entry in sorted(futures.items(), key=lambda item: item[0][1]):
            monthly = sorted(entry["monthly"])
            if name in INDICES:
                continue
            if exchange == "NFO":
                stocks.append(self._view(name, exchange, entry["lot_size"], [(d, "monthly") for d in monthly], today))
        contracts: dict[str, list] = {}
        index_futures = {name: sorted(futures[(exchange, name)]["contracts"], key=lambda c: c["expiry"])
                         for name, exchange in INDICES.items() if (exchange, name) in futures}
        for name, exchange in INDICES.items():
            entry = futures.get((exchange, name))
            monthly = set(entry["monthly"]) if entry else set()
            dates, lot = set(monthly), (entry or {}).get("lot_size", 0)
            with contextlib.suppress(KiteError):
                for row in self._rows(f"{exchange}:{name}", "underlying"):
                    expiry = _date(row.get("expiry_date"))
                    if expiry and expiry >= today and row.get("instrument_type") in ("CE", "PE"):
                        dates.add(expiry)
                        lot = lot or int(row.get("lot_size") or 0)
                        with contextlib.suppress(TypeError, ValueError, KeyError):
                            contracts.setdefault(name, []).append({
                                "exchange": row.get("exchange") or exchange, "tradingsymbol": str(row["tradingsymbol"]),
                                "underlying": name, "strike": float(row["strike"]), "type": row["instrument_type"],
                                "expiry": expiry, "lot_size": int(row.get("lot_size") or lot or 1),
                                "freeze_quantity": int(row.get("freeze_quantity") or 0),
                                "tick_size": float(row.get("tick_size") or 0) or 0.05})
            if dates:
                indices.append(self._view(name, exchange, lot, [(d, "monthly" if d in monthly else "weekly")
                                                                for d in sorted(dates)], today))
        metals, metal_note = self._metal_rows(today)
        with self._lock:
            self._data = {"fetched": self._now().strftime("%Y-%m-%d %H:%M"), "indices": indices, "stocks": stocks,
                          "contracts": contracts, "futures": index_futures, "mcx": metals, "mcx_note": metal_note}
            self._index_contracts()
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data), encoding="utf-8")
            tmp.replace(self.path)
        return self.view()

    def _metal_rows(self, today: str) -> tuple[dict, str | None]:
        """MCX gold and silver futures and options from Kite's instrument list, by metal. A failure here leaves the
        NSE/BSE list intact and is reported, never raised."""
        out: dict[str, list] = {}
        skipped = []
        try:
            for name in mcx.METALS:
                rows = self._rows(f"MCX:{name}", "underlying")
                if not any(r.get("exchange") == mcx.EXCHANGE for r in rows):
                    rows = self._rows(name, "name")
                seen = set()
                for row in rows:
                    kind = row.get("instrument_type")
                    symbol = str(row.get("tradingsymbol") or "").upper()
                    if (row.get("exchange") != mcx.EXCHANGE or str(row.get("name") or "").strip().upper() != name
                            or kind not in ("FUT", "CE", "PE") or not symbol or symbol in seen):
                        continue
                    expiry = _date(row.get("expiry_date") or row.get("expiry"))
                    if not expiry or expiry < today or mcx.name_of(symbol) != name:
                        continue
                    lot = int(row.get("lot_size") or 1)
                    if lot != 1:
                        skipped.append(symbol)          # Kite changed its quantity convention: value it by hand first
                        continue
                    seen.add(symbol)
                    with contextlib.suppress(TypeError, ValueError):
                        out.setdefault(name, []).append({
                            "exchange": mcx.EXCHANGE, "tradingsymbol": symbol, "underlying": name, "type": kind,
                            "strike": float(row["strike"]) if kind != "FUT" else None, "expiry": expiry, "lot_size": 1,
                            "multiplier": mcx.METALS[name]["units_per_lot"],
                            "freeze_quantity": int(row.get("freeze_quantity") or 0),
                            "tick_size": float(row.get("tick_size") or 0) or 1.0})
        except KiteError as exc:
            return out, f"MCX contracts could not be read from Kite: {exc}"
        for rows in out.values():
            rows.sort(key=lambda c: (c["expiry"], c["type"] != "FUT", c["strike"] or 0, c["type"]))
        if skipped:
            return out, (f"Kite lists {len(skipped)} MCX contract(s) with a lot size other than 1 ({', '.join(skipped[:3])}"
                         f"…); they are left out until the desk is updated for it.")
        return out, None if out else "Kite returned no MCX gold or silver contracts. Is the commodity segment active?"

    def _index_contracts(self) -> None:
        """Every option, index future and MCX contract by its Kite name, so an order for any can be checked."""
        self._by_symbol = {(c["exchange"], c["tradingsymbol"]): c
                           for group in ("contracts", "futures", "mcx") for rows in self._data.get(group, {}).values()
                           for c in rows}

    def metal_contracts(self, name: str) -> list[dict]:
        """An MCX metal's live contracts (futures and options), nearest expiry first."""
        today = self._now().strftime("%Y-%m-%d")
        return [c for c in self._data.get("mcx", {}).get(name, []) if c["expiry"] >= today]

    def metal_futures(self, name: str) -> list[dict]:
        return [c for c in self.metal_contracts(name) if c["type"] == "FUT"]

    def metals(self) -> dict:
        """Every MCX metal the desk trades: its spec, its futures, and the expiries that have options."""
        out = []
        for name, spec in mcx.METALS.items():
            rows = self.metal_contracts(name)
            out.append({"name": name, **spec, "exchange": mcx.EXCHANGE, "futures": [c for c in rows if c["type"] == "FUT"],
                        "option_expiries": sorted({c["expiry"] for c in rows if c["type"] != "FUT"})})
        return {"fetched": self._data.get("fetched"), "metals": out, "note": self._data.get("mcx_note"),
                "connected": bool(self.kite.connected and "search_instruments" in self.kite.tools)}

    def contract(self, exchange: str, tradingsymbol: str) -> dict | None:
        return self._by_symbol.get((exchange.upper(), tradingsymbol.upper()))

    def futures(self) -> list[dict]:
        """Index futures that have not expired, nearest first within each index."""
        today = self._now().strftime("%Y-%m-%d")
        order = list(INDICES)
        rows = [c for name, items in self._data.get("futures", {}).items() for c in items if c["expiry"] >= today]
        return sorted(rows, key=lambda c: (order.index(c["underlying"]) if c["underlying"] in order else 99, c["expiry"]))

    def future(self, exchange: str, tradingsymbol: str) -> dict | None:
        return next((c for c in self.futures() if c["exchange"] == exchange.upper()
                     and c["tradingsymbol"] == tradingsymbol.upper()), None)

    def contracts(self, underlying: str) -> list[dict]:
        return list(self._data.get("contracts", {}).get(underlying, []))

    def underlyings(self) -> list[dict]:
        """Indices with options: name, exchange, lot size and the expiries that have contracts."""
        today = self._now().strftime("%Y-%m-%d")
        out = []
        for name, rows in self._data.get("contracts", {}).items():
            expiries = sorted({c["expiry"] for c in rows if c["expiry"] >= today})
            if expiries:
                out.append({"name": name, "exchange": rows[0]["exchange"], "lot_size": rows[0]["lot_size"],
                            "expiries": expiries})
        order = list(INDICES)
        return sorted(out, key=lambda u: order.index(u["name"]) if u["name"] in order else 99)

    @staticmethod
    def _view(name, exchange, lot_size, expiries, today) -> dict:
        return {"name": name, "exchange": exchange, "lot_size": lot_size,
                "expiries": [{"date": date, "type": kind} for date, kind in expiries[:8]]}

    def stale(self) -> bool:
        """Whether the list needs rebuilding: never fetched, fetched before today, or holding an expired date."""
        with self._lock:
            fetched = self._data.get("fetched")
            today = self._now().strftime("%Y-%m-%d")
            if not fetched or fetched[:10] < today:
                return True
            return any(e["date"] < today for group in ("indices", "stocks") for item in self._data[group]
                       for e in item["expiries"][:1])

    def view(self) -> dict:
        """The list as of now: expired dates dropped, days to each expiry counted from today."""
        today = pd.Timestamp(self._now().date())
        with self._lock:
            out = {"fetched": self._data.get("fetched"), "stale": False, "indices": [], "stocks": []}
            for group in ("indices", "stocks"):
                for item in self._data[group]:
                    live = [{**e, "days": int((pd.Timestamp(e["date"]) - today).days),
                             "weekday": pd.Timestamp(e["date"]).strftime("%a")}
                            for e in item["expiries"] if pd.Timestamp(e["date"]) >= today]
                    if live:
                        out[group].append({**item, "expiries": live, "next": live[0]})
        out["stale"] = self.stale()
        out["connected"] = bool(self.kite.connected and "search_instruments" in self.kite.tools)
        return out

    def tick(self) -> None:
        """Rebuild once the list is out of date and Kite is there to ask."""
        if self.kite.connected and "search_instruments" in self.kite.tools and self.stale():
            with contextlib.suppress(KiteError):
                self.refresh()
