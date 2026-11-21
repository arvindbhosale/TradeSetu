"""Option chain history: NSE's daily F&O bhavcopy for index options, and live chain snapshots from the broker.

Brokers keep no candles for expired option contracts, so the option strategy
backtest used to model every premium. This module keeps real ones:

- **NSE bhavcopy** (end of day). NSE publishes one file per trading day with
  every F&O contract's open, high, low, close, settlement price, open interest
  and contracts traded. Two formats: until July 2024
  ``content/historical/DERIVATIVES/<YYYY>/<MON>/fo<DD><MON><YYYY>bhav.csv.zip``;
  from 8 July 2024 the UDiFF file ``content/fo/BhavCopy_NSE_FO_0_0_0_<YYYYMMDD>_F_0000.csv.zip``.
  ``import_range`` downloads a period day by day, keeps the index options and
  futures of the chosen underlyings (strikes within ``band_pct`` of the near
  future, expiries within ``max_days``) and records each day's outcome, so a
  run that stops resumes where it left off. A missing file (404) on a past
  weekday is a holiday; NSE refusing (403) is an error and is never stored as
  data. NSE blocks many cloud addresses: run it on your own computer, or set
  ``DESK_EXCHANGE_PROXY`` (the client in ``nse.py`` sends every request
  through it).
- **Live snapshots** (intraday, from the day it is switched on). ``Recorder``
  saves the near expiries of the chosen indices from the broker every few minutes
  during the session: last price, bid, ask, open interest, volume and IV.

Everything lives in one SQLite file, ``option_history.db`` in the desk's home
folder. Prices are stored in paise, strikes in paise, dates as YYYYMMDD
integers and snapshot times as YYYYMMDDHHMM (IST).

Command line (from your own computer, or a server with the proxy set)::

    python -m desk.chainhistory import --from 2016-01-01 --to 2026-11-20 --underlyings NIFTY,BANKNIFTY,FINNIFTY
    python -m desk.chainhistory status
"""

from __future__ import annotations

import argparse
import csv
import io
import logging
import sqlite3
import threading
import zipfile
from collections import OrderedDict
from datetime import date, datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

ARCHIVE = "https://nsearchives.nseindia.com"
UDIFF_FROM = date(2024, 7, 8)          # NSE's first UDiFF-only F&O bhavcopy
DEFAULT_UNDERLYINGS = ("NIFTY", "BANKNIFTY", "FINNIFTY")
INDEX_OPTIONS = ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50")   # NSE index options
# Saturday sessions NSE held as full trading days (Union Budget days, and 20 January 2024). A wrong guess costs one 404.
SPECIAL_SESSIONS = frozenset({date(2020, 2, 1), date(2024, 1, 20), date(2025, 2, 1)})
STOP_AFTER_ERRORS = 5                  # consecutive refusals before an import gives up (blocked address)
MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")
RECENT_DAYS = 3                        # a 404 this recent may only mean "not published yet": tried again later

SCHEMA = """
CREATE TABLE IF NOT EXISTS opt (u TEXT, d INTEGER, e INTEGER, k INTEGER, t TEXT, o INTEGER, h INTEGER, l INTEGER,
    c INTEGER, s INTEGER, oi INTEGER, v INTEGER, PRIMARY KEY (u, d, e, k, t)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS fut (u TEXT, d INTEGER, e INTEGER, o INTEGER, h INTEGER, l INTEGER, c INTEGER,
    oi INTEGER, PRIMARY KEY (u, d, e)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS days (d INTEGER PRIMARY KEY, status TEXT, source TEXT, rows INTEGER, note TEXT, at TEXT);
CREATE TABLE IF NOT EXISTS snap (u TEXT, ts INTEGER, e INTEGER, k INTEGER, t TEXT, ltp INTEGER, bid INTEGER,
    ask INTEGER, oi INTEGER, v INTEGER, iv REAL, spot INTEGER, PRIMARY KEY (u, ts, e, k, t)) WITHOUT ROWID;
"""


class Blocked(RuntimeError):
    """NSE refused the downloads: the address is blocked or the network is down."""


def paise(value) -> int:
    text = str(value).strip() if value is not None else ""
    if not text or text in ("-", "nan"):
        return 0
    return int(round(float(text) * 100))


def ymd(day: date) -> int:
    return day.year * 10000 + day.month * 100 + day.day


def from_ymd(value: int) -> date:
    return date(value // 10000, value // 100 % 100, value % 100)


def urls(day: date) -> list[tuple[str, str]]:
    """The bhavcopy addresses for ``day``, the format NSE used then first."""
    mon = MONTHS[day.month - 1]
    old = f"{ARCHIVE}/content/historical/DERIVATIVES/{day:%Y}/{mon}/fo{day:%d}{mon}{day:%Y}bhav.csv.zip"
    new = f"{ARCHIVE}/content/fo/BhavCopy_NSE_FO_0_0_0_{day:%Y%m%d}_F_0000.csv.zip"
    pair = [("udiff", new), ("old", old)]
    return pair if day >= UDIFF_FROM else pair[::-1]


def _csv_text(raw: bytes) -> str:
    if raw[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            names = [n for n in archive.namelist() if n.lower().endswith(".csv")]
            if not names:
                raise ValueError("the zip holds no CSV")
            raw = archive.read(names[0])
    return raw.decode("utf-8-sig", errors="replace")


def _expiry(text: str) -> int:
    text = text.strip()
    for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%d-%B-%Y", "%d%b%Y"):
        try:
            return ymd(datetime.strptime(text, fmt).date())
        except ValueError:
            continue
    raise ValueError(f"unknown date {text!r}")


def parse(raw: bytes, underlyings, band_pct: float = 15.0, max_days: int = 100) -> dict:
    """One bhavcopy (zip or CSV, either format) to ``{"day", "format", "opt": [...], "fut": [...]}``.

    Option rows: (u, d, e, k, t, o, h, l, c, s, oi, v), prices and strikes in paise. Kept: the index options of
    ``underlyings`` whose expiry is within ``max_days``, whose strike is within ``band_pct`` of the near future and
    that traded or had open interest that day (a strike with neither has only a theoretical price).
    """
    text = _csv_text(raw)
    reader = csv.reader(io.StringIO(text))
    header = [h.strip() for h in next(reader, [])]
    col = {name: i for i, name in enumerate(header)}
    wanted = {u.upper() for u in underlyings}
    if "FinInstrmTp" in col:
        fmt = "udiff"
        f = {"kind": "FinInstrmTp", "sym": "TckrSymb", "exp": "XpryDt", "strike": "StrkPric", "type": "OptnTp",
             "o": "OpnPric", "h": "HghPric", "l": "LwPric", "c": "ClsPric", "s": "SttlmPric", "oi": "OpnIntrst",
             "v": "TtlTradgVol", "day": "TradDt"}
        option_kinds, future_kinds = {"IDO"}, {"IDF"}
    elif "INSTRUMENT" in col:
        fmt = "old"
        f = {"kind": "INSTRUMENT", "sym": "SYMBOL", "exp": "EXPIRY_DT", "strike": "STRIKE_PR", "type": "OPTION_TYP",
             "o": "OPEN", "h": "HIGH", "l": "LOW", "c": "CLOSE", "s": "SETTLE_PR", "oi": "OPEN_INT", "v": "CONTRACTS",
             "day": "TIMESTAMP"}
        option_kinds, future_kinds = {"OPTIDX"}, {"FUTIDX"}
    else:
        raise ValueError("not an NSE F&O bhavcopy (unknown columns)")
    missing = [name for name in f.values() if name not in col]
    if missing:
        raise ValueError(f"bhavcopy lacks columns {', '.join(missing)}")
    idx = {key: col[name] for key, name in f.items()}
    width = max(idx.values()) + 1
    options, futures, day = [], [], None
    for row in reader:
        if len(row) < width:
            continue
        kind, sym = row[idx["kind"]].strip(), row[idx["sym"]].strip().upper()
        if sym not in wanted or kind not in option_kinds | future_kinds:
            continue
        if day is None:
            day = _expiry(row[idx["day"]])
        expiry = _expiry(row[idx["exp"]])
        if kind in future_kinds:
            futures.append((sym, day, expiry, paise(row[idx["o"]]), paise(row[idx["h"]]), paise(row[idx["l"]]),
                            paise(row[idx["c"]]), int(float(row[idx["oi"]] or 0))))
            continue
        kind_text = row[idx["type"]].strip().upper()
        if kind_text not in ("CE", "PE"):
            continue
        options.append((sym, day, expiry, paise(row[idx["strike"]]), kind_text, paise(row[idx["o"]]), paise(row[idx["h"]]),
                        paise(row[idx["l"]]), paise(row[idx["c"]]), paise(row[idx["s"]]),
                        int(float(row[idx["oi"]] or 0)), int(float(row[idx["v"]] or 0))))
    if day is None:
        return {"day": None, "format": fmt, "opt": [], "fut": []}
    near = {}
    for u, _, _e, _, _, _, c, _ in sorted(futures, key=lambda r: r[2]):
        if c > 0:
            near.setdefault(u, c)
    last = ymd(from_ymd(day) + timedelta(days=max_days))
    kept = []
    for r in options:
        if r[2] > last or r[2] < day or (r[10] == 0 and r[11] == 0):     # no trade and no open interest: no price
            continue
        ref = near.get(r[0])
        if ref and abs(r[3] - ref) > ref * band_pct / 100:
            continue
        kept.append(r)
    return {"day": day, "format": fmt, "opt": kept, "fut": futures}


def _http_status(exc: Exception) -> int | None:
    response = getattr(exc, "response", None)
    return getattr(response, "status_code", None)


class ChainHistory:
    """The SQLite store: imports, coverage and lookups. Thread-safe."""

    def __init__(self, path: Path | str, client=None, now=None):
        self.path = Path(path)
        self._client = client
        self._now = now or datetime.now
        self._lock = threading.RLock()
        self._db: sqlite3.Connection | None = None
        self._cache: OrderedDict = OrderedDict()
        self.job: dict = {"running": False}
        self._stop = threading.Event()

    # -- storage --------------------------------------------------------------------------------------------------

    def _conn(self) -> sqlite3.Connection:
        if self._db is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._db = sqlite3.connect(self.path, check_same_thread=False)
            self._db.executescript(SCHEMA)
            self._db.execute("PRAGMA journal_mode=WAL")
        return self._db

    def exists(self) -> bool:
        return self.path.exists()

    def store(self, parsed: dict, source: str) -> int:
        with self._lock:
            db = self._conn()
            with db:
                db.executemany("INSERT OR REPLACE INTO opt VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", parsed["opt"])
                db.executemany("INSERT OR REPLACE INTO fut VALUES (?,?,?,?,?,?,?,?)", parsed["fut"])
                db.execute("INSERT OR REPLACE INTO days VALUES (?,?,?,?,?,?)",
                           (parsed["day"], "ok", source, len(parsed["opt"]), "", self._now().isoformat(timespec="seconds")))
            self._cache.clear()
        return len(parsed["opt"])

    def _mark(self, day: date, status: str, note: str = "") -> None:
        with self._lock:
            db = self._conn()
            with db:
                db.execute("INSERT OR REPLACE INTO days VALUES (?,?,?,?,?,?)",
                           (ymd(day), status, "", 0, note[:300], self._now().isoformat(timespec="seconds")))

    def done_days(self) -> dict[int, str]:
        with self._lock:
            return dict(self._conn().execute("SELECT d, status FROM days"))

    # -- import ---------------------------------------------------------------------------------------------------

    @property
    def client(self):
        if self._client is None:
            from .nse import NseClient
            self._client = NseClient(gap=1.0, timeout=60.0)
        return self._client

    def _download(self, url: str) -> bytes | None:
        """The file, or None when NSE has none (404). Anything else raises."""
        try:
            return self.client.file(url)
        except Exception as exc:  # noqa: BLE001 - sorted below
            if _http_status(exc) == 404 or isinstance(exc, FileNotFoundError):
                return None
            raise

    def import_day(self, day: date, underlyings=DEFAULT_UNDERLYINGS, band_pct: float = 15.0, max_days: int = 100) -> str:
        """Fetch and store one day. Returns "ok", "none" (no file: a holiday) or "pending" (too recent to tell)."""
        for source, url in urls(day):
            raw = self._download(url)
            if raw is None:
                continue
            parsed = parse(raw, underlyings, band_pct, max_days)
            if parsed["day"] is None:
                raise ValueError(f"{source} bhavcopy for {day} has none of {', '.join(underlyings)}")
            if parsed["day"] != ymd(day):
                raise ValueError(f"the {day} bhavcopy is dated {from_ymd(parsed['day'])}")
            self.store(parsed, source)
            return "ok"
        if (self._now().date() - day).days <= RECENT_DAYS:
            return "pending"
        self._mark(day, "none", "NSE has no bhavcopy for this day (holiday)")
        return "none"

    @staticmethod
    def sessions(start: date, end: date) -> list[date]:
        out, day = [], start
        while day <= end:
            if day.weekday() < 5 or day in SPECIAL_SESSIONS:
                out.append(day)
            day += timedelta(days=1)
        return out

    def import_range(self, start: date, end: date, underlyings=DEFAULT_UNDERLYINGS, band_pct: float = 15.0,
                     max_days: int = 100, retry_errors: bool = True, progress=None) -> dict:
        """Import every weekday from ``start`` to ``end`` not yet done. Stops after ``STOP_AFTER_ERRORS`` refusals
        in a row (raises ``Blocked``) or when ``stop()`` is called."""
        self._stop.clear()
        done = self.done_days()
        todo = [d for d in self.sessions(start, end)
                if done.get(ymd(d)) not in ("ok", "none") and (retry_errors or done.get(ymd(d)) != "error")]
        counts = {"ok": 0, "none": 0, "pending": 0, "error": 0, "todo": len(todo), "skipped": 0}
        streak, last_error = 0, ""
        for n, day in enumerate(todo, 1):
            if self._stop.is_set():
                counts["stopped"] = True
                break
            try:
                outcome = self.import_day(day, underlyings, band_pct, max_days)
                counts[outcome] += 1
                streak = 0
            except Exception as exc:  # noqa: BLE001 - recorded per day; a run of them means a block
                last_error = f"{type(exc).__name__}: {str(exc)[:200]}"
                status = _http_status(exc)
                self._mark(day, "error", last_error)
                counts["error"] += 1
                # A bad file is that day's problem; refusals and network failures in a row mean we are blocked.
                if not isinstance(exc, (ValueError, zipfile.BadZipFile)) or status in (401, 403):
                    streak += 1
                if streak >= STOP_AFTER_ERRORS:
                    raise Blocked(f"NSE refused {streak} days in a row (last: {last_error}). NSE blocks many cloud "
                                  "and data-centre addresses: run the import on your own computer, or set "
                                  "DESK_EXCHANGE_PROXY to a proxy on an Indian home connection.") from exc
            if progress:
                progress(n, len(todo), day, counts)
        counts["last_error"] = last_error
        return counts

    def stop(self) -> None:
        self._stop.set()

    def start_import(self, start: date, end: date, underlyings=DEFAULT_UNDERLYINGS, **kwargs) -> dict:
        """Run ``import_range`` on a background thread; ``job`` shows its progress."""
        with self._lock:
            if self.job.get("running"):
                raise ValueError("An import is already running.")
            self.job = {"running": True, "start": start.isoformat(), "end": end.isoformat(),
                        "underlyings": list(underlyings), "done": 0, "total": None, "day": None, "counts": {},
                        "error": None, "started": self._now().isoformat(timespec="seconds"), "finished": None}

        def progress(n, total, day, counts):
            self.job.update(done=n, total=total, day=day.isoformat(), counts=dict(counts))

        def run():
            try:
                counts = self.import_range(start, end, underlyings, progress=progress, **kwargs)
                self.job.update(counts=counts)
            except Exception as exc:  # noqa: BLE001 - shown on the page
                logger.warning("option history import stopped: %s", exc)
                self.job.update(error=str(exc))
            finally:
                self.job.update(running=False, finished=self._now().isoformat(timespec="seconds"))

        threading.Thread(target=run, name="chain-history-import", daemon=True).start()
        return self.job

    # -- coverage -------------------------------------------------------------------------------------------------

    def status(self) -> dict:
        if not self.exists():
            return {"underlyings": [], "days": {}, "snapshots": [], "size_mb": 0.0, "job": self.job}
        with self._lock:
            db = self._conn()
            cover = [{"underlying": u, "first": str(from_ymd(a)), "last": str(from_ymd(b)), "days": n, "rows": r}
                     for u, a, b, n, r in db.execute(
                         "SELECT u, MIN(d), MAX(d), COUNT(DISTINCT d), COUNT(*) FROM opt GROUP BY u ORDER BY u")]
            days = dict(db.execute("SELECT status, COUNT(*) FROM days GROUP BY status"))
            errors = [{"day": str(from_ymd(d)), "note": note} for d, note in
                      db.execute("SELECT d, note FROM days WHERE status='error' ORDER BY d DESC LIMIT 5")]
            snaps = [{"underlying": u, "first": _stamp_text(a), "last": _stamp_text(b), "snapshots": n}
                     for u, a, b, n in db.execute(
                         "SELECT u, MIN(ts), MAX(ts), COUNT(DISTINCT ts) FROM snap GROUP BY u ORDER BY u")]
        size = sum(p.stat().st_size for p in self.path.parent.glob(self.path.name + "*") if p.is_file())
        return {"underlyings": cover, "days": days, "errors": errors, "snapshots": snaps,
                "size_mb": round(size / 2**20, 1), "job": self.job}

    def covers(self, underlying: str, start: date, end: date) -> int:
        """Trading days of ``underlying`` stored between ``start`` and ``end``."""
        if not self.exists():
            return 0
        with self._lock:
            row = self._conn().execute("SELECT COUNT(DISTINCT d) FROM opt WHERE u=? AND d BETWEEN ? AND ?",
                                       (underlying, ymd(start), ymd(end))).fetchone()
            snaps = self._conn().execute("SELECT COUNT(*) FROM snap WHERE u=? AND ts BETWEEN ? AND ?",
                                         (underlying, ymd(start) * 10000, ymd(end) * 10000 + 2359)).fetchone()
        return int(row[0] or 0) + (1 if snaps and snaps[0] else 0)

    # -- lookups for the backtest ---------------------------------------------------------------------------------

    def _day(self, underlying: str, day: int) -> dict:
        key = ("eod", underlying, day)
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                return self._cache[key]
            rows = self._conn().execute("SELECT e, k, t, o, h, l, c, s, oi, v FROM opt WHERE u=? AND d=?",
                                        (underlying, day)).fetchall()
            value = {(r[0], r[1], r[2]): r[3:] for r in rows}
            self._cache[key] = value
            while len(self._cache) > 64:
                self._cache.popitem(last=False)
            return value

    def expiries(self, underlying: str, day: date) -> list[date]:
        """Every expiry NSE listed for ``underlying`` on ``day`` (within the stored band), soonest first."""
        return [from_ymd(e) for e in sorted({e for e, _, _ in self._day(underlying, ymd(day))})]

    def eod(self, underlying: str, day: date, expiry: date, strike: float, kind: str) -> dict | None:
        """The day's bhavcopy row in rupees: open, high, low, close, settle, oi, volume (contracts)."""
        row = self._day(underlying, ymd(day)).get((ymd(expiry), int(round(strike * 100)), kind))
        if row is None:
            return None
        first, high, low, close, settle, oi, volume = row
        return {"open": first / 100, "high": high / 100, "low": low / 100, "close": close / 100, "settle": settle / 100,
                "oi": oi, "volume": volume}

    def _snaps(self, underlying: str, day: int) -> dict:
        key = ("snap", underlying, day)
        with self._lock:
            if key in self._cache:
                return self._cache[key]
            rows = self._conn().execute("SELECT ts, e, k, t, ltp FROM snap WHERE u=? AND ts BETWEEN ? AND ?",
                                        (underlying, day * 10000, day * 10000 + 2359)).fetchall()
            out: dict = {}
            for ts, e, k, t, ltp in rows:
                if ltp:
                    out.setdefault((e, k, t), []).append((ts % 10000, ltp / 100))
            for series in out.values():
                series.sort()
            self._cache[key] = out
            while len(self._cache) > 64:
                self._cache.popitem(last=False)
            return out

    def snapshot(self, underlying: str, moment: datetime, expiry: date, strike: float, kind: str,
                 within_minutes: int = 10) -> float | None:
        """The last recorded price at or before ``moment`` (IST wall clock), if no older than ``within_minutes``."""
        series = self._snaps(underlying, ymd(moment.date())).get((ymd(expiry), int(round(strike * 100)), kind))
        if not series:
            return None
        hhmm = moment.hour * 100 + moment.minute
        best = None
        for at, price in series:
            if at > hhmm:
                break
            best = (at, price)
        if best is None:
            return None
        gap = (hhmm // 100 * 60 + hhmm % 100) - (best[0] // 100 * 60 + best[0] % 100)
        return best[1] if gap <= within_minutes else None

    # -- live snapshots -------------------------------------------------------------------------------------------

    def record(self, underlying: str, moment: datetime, expiry: str, chain: dict) -> int:
        """Store one ``options.build_chain`` result taken at ``moment`` (IST)."""
        ts = int(moment.strftime("%Y%m%d%H%M"))
        e = int(expiry.replace("-", ""))
        spot = paise(chain.get("spot"))
        rows = []
        for line in chain.get("rows") or []:
            for kind in ("CE", "PE"):
                x = line.get(kind)
                if not x or not x.get("ltp"):
                    continue
                rows.append((underlying, ts, e, paise(line["strike"]), kind, paise(x["ltp"]), paise(x.get("bid")),
                             paise(x.get("ask")), int(x.get("oi") or 0), int(x.get("volume") or 0), x.get("iv"), spot))
        with self._lock:
            db = self._conn()
            with db:
                db.executemany("INSERT OR REPLACE INTO snap VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
            self._cache.pop(("snap", underlying, ts // 10000), None)
        return len(rows)


def _stamp_text(ts: int | None) -> str | None:
    if not ts:
        return None
    text = str(ts)
    return f"{text[:4]}-{text[4:6]}-{text[6:8]} {text[8:10]}:{text[10:12]}"


class Recorder:
    """Every ``chain_recorder_minutes`` in the session, store the near two expiries of each index in the
    ``chain_recorder`` setting from the broker: one quote call per index (up to 500 instruments; ATM ± 20 strikes is 164)
    and one price call, far inside broker rate limits."""

    WIDTH = 20
    EXPIRIES = 2

    def __init__(self, history: ChainHistory, kite, derivatives, settings, now=None):
        from . import market
        self.history, self.kite, self.derivatives, self.settings = history, kite, derivatives, settings
        self._now = now or market.now_ist
        self._next: dict[str, datetime] = {}
        self.last: dict[str, dict] = {}

    def tick(self) -> None:
        from . import market, options as opt
        underlyings = self.settings.get("chain_recorder") or []
        now = self._now()
        if not underlyings or not market.is_trading_day(now) or not (market.OPEN <= now.time() <= market.CLOSE):
            return
        if not self.kite.connected or "get_quotes" not in self.kite.tools:
            return
        minutes = int(self.settings.get("chain_recorder_minutes") or 5)
        for underlying in underlyings:
            due = self._next.get(underlying)
            if due is not None and now < due:
                continue
            self._next[underlying] = now.replace(second=0, microsecond=0) + timedelta(minutes=minutes)
            contracts = self.derivatives.contracts(underlying)
            if not contracts:
                continue
            today = now.strftime("%Y-%m-%d")
            expiries = sorted({c["expiry"] for c in contracts if c["expiry"] >= today})[: self.EXPIRIES]
            spot_name = opt.SPOT[underlying]
            spot = self.kite.ltp([spot_name]).get(spot_name)
            if not spot or not expiries:
                continue
            wanted = []
            for expiry in expiries:
                near = sorted({c["strike"] for c in contracts if c["expiry"] == expiry},
                              key=lambda k: abs(k - spot))[: 2 * self.WIDTH + 1]
                wanted += [f"{c['exchange']}:{c['tradingsymbol']}" for c in contracts
                           if c["expiry"] == expiry and c["strike"] in near]
            quotes: dict = {}
            for begin in range(0, len(wanted), 500):
                quotes.update(self.kite.quotes(wanted[begin:begin + 500]))
            stored = 0
            for expiry in expiries:
                chain = opt.build_chain(contracts, quotes, spot, expiry, now, self.WIDTH)
                stored += self.history.record(underlying, now, expiry, chain)
            self.last[underlying] = {"at": now.strftime("%Y-%m-%d %H:%M"), "rows": stored}


# -- command line ----------------------------------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> None:
    from .settings import home

    parser = argparse.ArgumentParser(prog="python -m desk.chainhistory", description="NSE index option history")
    parser.add_argument("--db", default=None, help="SQLite file (default: option_history.db in the desk home)")
    sub = parser.add_subparsers(dest="command", required=True)
    imp = sub.add_parser("import", help="download NSE F&O bhavcopies for a period (resumes where it stopped)")
    imp.add_argument("--from", dest="start", required=True, help="first day, YYYY-MM-DD")
    imp.add_argument("--to", dest="end", default=None, help="last day, YYYY-MM-DD (default: yesterday)")
    imp.add_argument("--underlyings", default=",".join(DEFAULT_UNDERLYINGS))
    imp.add_argument("--band", type=float, default=15.0, help="keep strikes within this %% of the near future")
    imp.add_argument("--max-days", type=int, default=100, help="keep expiries up to this many days away")
    sub.add_parser("status", help="what is stored")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    store = ChainHistory(args.db or home() / "option_history.db")
    if args.command == "status":
        import json
        print(json.dumps(store.status(), indent=2))
        return
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end) if args.end else date.today() - timedelta(days=1)
    underlyings = [u.strip().upper() for u in args.underlyings.split(",") if u.strip()]

    def progress(n, total, day, counts):
        if n % 20 == 0 or n == total:
            print(f"{n}/{total} {day}  stored {counts['ok']}, holidays {counts['none']}, errors {counts['error']}", flush=True)

    try:
        counts = store.import_range(start, end, underlyings, args.band, args.max_days, progress=progress)
    except Blocked as exc:
        raise SystemExit(str(exc)) from None
    print(f"Done: {counts['ok']} days stored, {counts['none']} holidays, {counts['error']} errors"
          + (f" (last: {counts['last_error']})" if counts["error"] else ""))


if __name__ == "__main__":
    main()
