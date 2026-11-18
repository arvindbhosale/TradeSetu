"""One instrument list in Kite's shape, built from any broker's instrument file.

The desk names instruments as Kite does (``NSE:RELIANCE``, ``NFO:NIFTY25NOVFUT``, ``NFO:NIFTY25N1124000CE``,
``NSE:NIFTY 50``). Every other broker spells them differently, so symbols are never translated string to
string. Each broker's file is read into fields (exchange, underlying, kind, expiry, strike, option type) and
the Kite symbol is built from those fields. A contract keeps its broker's own key beside it, which is what
the broker's API is called with.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta, timezone

_IST = timezone(timedelta(hours=5, minutes=30))

# Kite's weekly-expiry month code: 1-9 for January to September, then O, N, D.
_WEEKLY_MONTH = "123456789OND"
_MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")

DERIVATIVE_EXCHANGE = {"NSE": "NFO", "BSE": "BFO", "MCX": "MCX", "NFO": "NFO", "BFO": "BFO", "CDS": "CDS",
                       "BCD": "BCD", "NCO": "NCO"}

# Index names as Kite spells them, with the other spellings brokers use (compared without spaces or symbols).
INDEX_ALIASES = {
    "NIFTY 50": ("NIFTY50", "NIFTY"),
    "NIFTY BANK": ("NIFTYBANK", "BANKNIFTY"),
    "NIFTY FIN SERVICE": ("NIFTYFINSERVICE", "FINNIFTY", "NIFTYFINANCIALSERVICES", "NIFTYFINSERV"),
    "NIFTY MID SELECT": ("NIFTYMIDSELECT", "MIDCPNIFTY", "NIFTYMIDCAPSELECT", "MIDCAPNIFTY"),
    "NIFTY NEXT 50": ("NIFTYNEXT50", "NIFTYNXT50", "NIFTYJR"),
    "INDIA VIX": ("INDIAVIX", "VIX"),
    "NIFTY 500": ("NIFTY500",),
    "NIFTY IT": ("NIFTYIT", "CNXIT"),
    "NIFTY AUTO": ("NIFTYAUTO", "CNXAUTO"),
    "NIFTY FMCG": ("NIFTYFMCG", "CNXFMCG"),
    "NIFTY PHARMA": ("NIFTYPHARMA", "CNXPHARMA"),
    "NIFTY METAL": ("NIFTYMETAL", "CNXMETAL"),
    "NIFTY REALTY": ("NIFTYREALTY", "CNXREALTY"),
    "NIFTY ENERGY": ("NIFTYENERGY", "CNXENERGY"),
    "NIFTY PSU BANK": ("NIFTYPSUBANK", "CNXPSUBANK"),
    "SENSEX": ("SENSEX", "BSESENSEX", "SPBSESENSEX", "BSE30"),
    "BANKEX": ("BANKEX", "BSEBANKEX", "SPBSEBANKEX"),
    "SENSEX50": ("SENSEX50", "BSESENSEX50", "SPBSESENSEX50"),
}
_ALIAS_TO_KITE = {alias: kite for kite, aliases in INDEX_ALIASES.items() for alias in aliases}


def squash(text: str) -> str:
    """Upper case letters and digits only: ``Nifty Fin Service`` → ``NIFTYFINSERVICE``."""
    return re.sub(r"[^A-Z0-9]", "", str(text or "").upper())


def index_symbol(*names: str) -> str:
    """Kite's name for an index, from whatever names a broker gives it (first match wins)."""
    for name in names:
        key = squash(re.sub(r"-INDEX$", "", str(name or "").upper()))
        if key in _ALIAS_TO_KITE:
            return _ALIAS_TO_KITE[key]
    for name in names:
        text = " ".join(re.sub(r"-INDEX$", "", str(name or "").upper()).split())
        if text:
            return text
    return ""


def strike_text(strike: float) -> str:
    """A strike as Kite writes it: ``24000``, ``7.5``, ``102.25``."""
    return f"{float(strike):.4f}".rstrip("0").rstrip(".")


def kite_symbol(underlying: str, kind: str, expiry: date, strike: float = 0.0, monthly: bool = True) -> str:
    """Kite's tradingsymbol for a future (``kind`` FUT) or an option (CE or PE).

    Monthly contracts read ``NIFTY25NOVFUT`` / ``NIFTY25NOV24000CE``; weekly options read
    ``NIFTY25N1124000CE`` (year, month code, day). Kite has no weekly futures.
    """
    year = f"{expiry.year % 100:02d}"
    if kind == "FUT":
        return f"{underlying}{year}{_MONTHS[expiry.month - 1]}FUT"
    if monthly:
        return f"{underlying}{year}{_MONTHS[expiry.month - 1]}{strike_text(strike)}{kind}"
    return f"{underlying}{year}{_WEEKLY_MONTH[expiry.month - 1]}{expiry.day:02d}{strike_text(strike)}{kind}"


def equity_symbol(symbol: str, exchange: str) -> str:
    """A cash-market symbol as Kite writes it: no ``-EQ`` (NSE) or BSE group suffix."""
    text = str(symbol or "").strip().upper()
    if exchange == "BSE":
        return re.sub(r"-(A|B|T|X|XT|Z|ZP|M|MT|MS|F|G|GC|IF|IT|E|P|R|W|TS|XD|Y)$", "", text)
    return re.sub(r"-EQ$", "", text)


@dataclass
class Contract:
    """One row of a broker's instrument file, in the fields the Kite symbol is built from."""

    exchange: str                 # Kite's exchange: NSE, BSE, NFO, BFO, MCX, CDS
    kind: str                     # EQ, INDEX, FUT, CE, PE
    key: str                      # the broker's own identifier for API calls
    symbol: str = ""              # cash market / index: the symbol; derivatives: ignored (built from fields)
    underlying: str = ""          # derivatives: NIFTY, RELIANCE, CRUDEOIL
    name: str = ""
    expiry: date | None = None
    strike: float = 0.0
    lot_size: int = 1
    tick_size: float = 0.05
    isin: str = ""
    freeze_quantity: int = 0
    weekly: bool | None = None    # when the broker says so; otherwise worked out from the expiry calendar
    native: dict = field(default_factory=dict)   # broker fields the adapter needs again (segment, token, ...)


class Master:
    """A broker's instruments, looked up by Kite symbol or by the broker's key."""

    def __init__(self, contracts: Iterable[Contract]):
        contracts = [c for c in contracts if c.exchange and c.kind and c.key]
        # The last expiry of each month per underlying is that month's monthly contract.
        last_in_month: dict[tuple, date] = {}
        for c in contracts:
            if c.expiry and c.kind in ("FUT", "CE", "PE"):
                slot = (c.exchange, c.underlying, c.expiry.year, c.expiry.month)
                if slot not in last_in_month or c.expiry > last_in_month[slot]:
                    last_in_month[slot] = c.expiry
        self.rows: list[dict] = []
        self.by_symbol: dict[str, dict] = {}
        self.by_key: dict[str, dict] = {}
        for c in contracts:
            row = self._row(c, last_in_month)
            if row is None:
                continue
            # The first row wins a symbol clash (a broker listing the same index twice, say).
            if row["id"] not in self.by_symbol:
                self.by_symbol[row["id"]] = row
                self.rows.append(row)
            self.by_key[c.key] = row

    @staticmethod
    def _row(c: Contract, last_in_month: dict) -> dict | None:
        if c.kind in ("FUT", "CE", "PE"):
            if not c.expiry or not c.underlying:
                return None
            if c.weekly is not None and c.exchange != "MCX":
                monthly = not c.weekly
            else:
                monthly = c.exchange == "MCX" or last_in_month.get(
                    (c.exchange, c.underlying, c.expiry.year, c.expiry.month)) == c.expiry
            symbol = kite_symbol(c.underlying, c.kind, c.expiry, c.strike, monthly)
            segment = f"{c.exchange}-{'FUT' if c.kind == 'FUT' else 'OPT'}"
            name = c.underlying
        elif c.kind == "INDEX":
            symbol = index_symbol(c.symbol, c.name)
            segment, name = "INDICES", c.name or symbol
        else:
            symbol, segment, name = equity_symbol(c.symbol, c.exchange), c.exchange, c.name
        if not symbol:
            return None
        return {
            "id": f"{c.exchange}:{symbol}", "exchange": c.exchange, "tradingsymbol": symbol, "name": name,
            "instrument_type": "EQ" if c.kind == "INDEX" else c.kind, "segment": segment,
            "expiry_date": c.expiry.isoformat() if c.expiry else "", "strike": float(c.strike or 0),
            "lot_size": int(c.lot_size or 1), "tick_size": float(c.tick_size or 0.05), "isin": c.isin,
            "freeze_quantity": int(c.freeze_quantity or 0), "instrument_token": c.key,
            "underlying": c.underlying, "_native": c.native,
        }

    def __len__(self) -> int:
        return len(self.rows)

    def get(self, instrument: str) -> dict | None:
        """The row for ``EXCHANGE:SYMBOL`` as Kite spells it."""
        exchange, _, symbol = str(instrument).partition(":")
        row = self.by_symbol.get(f"{exchange.upper()}:{symbol.upper()}")
        if row is None and exchange.upper() in ("NSE", "BSE"):
            alias = index_symbol(symbol)
            row = self.by_symbol.get(f"{exchange.upper()}:{alias}")
        return row

    def search(self, query: str, filter_on: str = "id") -> list[dict]:
        """Rows matching as kite-mcp-server's ``search_instruments`` matches them (substring, case-insensitive).

        ``underlying`` takes ``EXCHANGE:NAME`` or ``NAME`` and returns that underlying's futures and options.
        """
        q = str(query or "").strip().upper()
        if not q:
            return []
        if filter_on == "underlying":
            exchange, _, name = q.rpartition(":")
            exchange = DERIVATIVE_EXCHANGE.get(exchange, exchange)
            return [public(r) for r in self.rows if r["instrument_type"] in ("FUT", "CE", "PE")
                    and r["name"].upper() == name and (not exchange or r["exchange"] == exchange)]
        key = {"id": "id", "name": "name", "tradingsymbol": "tradingsymbol", "isin": "isin"}.get(filter_on, "id")
        return [public(r) for r in self.rows if q in str(r.get(key) or "").upper()]


def public(row: dict) -> dict:
    """A row as the desk sees it, without the broker's private fields."""
    return {k: v for k, v in row.items() if not k.startswith("_")}


class MasterCache:
    """Loads a broker's instrument file once a day, on first use."""

    def __init__(self, load):
        self._load, self._lock = load, threading.Lock()
        self._master: Master | None = None
        self._day: str | None = None

    def get(self, today: str) -> Master:
        with self._lock:
            if self._master is None or self._day != today:
                self._master, self._day = Master(self._load()), today
            return self._master

    def clear(self) -> None:
        with self._lock:
            self._master = self._day = None


def parse_date(value) -> date | None:
    """An expiry in any of the brokers' formats: epoch seconds or milliseconds, ``2025-11-25``,
    ``2025-11-25 14:30:00``, ``25NOV2025``, ``25-Nov-2025``, ``25/11/2025``."""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in ("nan", "none", "null", "0", "-1"):
        return None
    if re.fullmatch(r"\d{9,13}(\.0+)?", text):
        number = float(text)
        seconds = number / 1000 if number > 1e11 else number
        ist = datetime.fromtimestamp(seconds, tz=UTC).astimezone(_IST)
        return ist.date()
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%d%b%Y", "%d-%b-%Y", "%d/%m/%Y", "%d-%m-%Y",
                "%Y%m%d", "%d %b %Y"):
        try:
            return datetime.strptime(text.split(".")[0], fmt).date()
        except ValueError:
            continue
    match = re.match(r"(\d{4}-\d{2}-\d{2})", text)
    if match:
        return datetime.strptime(match.group(1), "%Y-%m-%d").date()
    return None


def number(value, default: float = 0.0) -> float:
    try:
        out = float(str(value).strip())
    except (TypeError, ValueError):
        return default
    return default if out != out else out


def integer(value, default: int = 0) -> int:
    return int(number(value, default))

