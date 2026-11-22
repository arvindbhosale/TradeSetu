"""Charts from an OHLC file the user imports, for NSE/BSE symbols while no broker is connected.

A file needs a date (or date and time) column and Open, High, Low, Close; Volume is optional. Dates may be
ISO (2026-11-20, 2026-11-20 09:15), day first (20-11-2026, 20/11/2026) or Unix seconds/milliseconds. Times
are read as India time unless the file gives a time zone. Rows with a missing or impossible price are
dropped and counted; a file where more than one row in ten is bad is refused, with the first bad lines named.
Imported candles are never live.
"""
from __future__ import annotations

import io
import json
import re
import time
from pathlib import Path

import pandas as pd

from .. import data
from . import MAX_BARS, TIMEFRAMES, MarketDataError, check_timeframe, rows_from_frame, weekly

MAX_BYTES = 5_000_000
MAX_FILES = 50
NAME = re.compile(r"[A-Z0-9][A-Z0-9&._\-]{0,29}")
DATE_NAMES = ("datetime", "date time", "timestamp", "date", "time")
COLUMNS = ("Open", "High", "Low", "Close", "Volume")


def _columns(frame: pd.DataFrame) -> dict:
    return {str(c).strip().lower().replace("_", " "): c for c in frame.columns}


def _when(frame: pd.DataFrame, names: dict) -> tuple[pd.Series, str]:
    date_col = next((names[n] for n in DATE_NAMES if n in names), None)
    if date_col is None:
        raise ValueError("The file needs a Date (or Datetime) column.")
    raw = frame[date_col]
    if names.get("date") is not None and names.get("time") is not None and date_col == names["date"]:
        raw = frame[names["date"]].astype(str).str.strip() + " " + frame[names["time"]].astype(str).str.strip()
    numeric = pd.to_numeric(raw, errors="coerce")
    if numeric.notna().all() and len(numeric) and numeric.min() > 1e9:
        unit = "ms" if numeric.min() > 1e12 else "s"
        stamps = pd.to_datetime(numeric, unit=unit, utc=True)
        return stamps.dt.tz_convert("Asia/Kolkata").dt.tz_localize(None), f"Unix {('milli' if unit == 'ms' else '')}seconds"
    text = raw.astype(str).str.strip()
    iso = pd.to_datetime(text, errors="coerce", format="ISO8601")
    how = "year first (ISO)"
    if iso.isna().mean() > 0.1:
        iso, how = pd.to_datetime(text, errors="coerce", dayfirst=True, format="mixed"), "day first"
    if getattr(iso.dt, "tz", None) is not None:
        iso = iso.dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
    return iso, how


def parse(text: str) -> tuple[pd.DataFrame, dict]:
    """Bars indexed by IST wall-clock time, and a report. Raises ValueError with what is wrong."""
    if not text or not text.strip():
        raise ValueError("The file is empty.")
    if len(text.encode("utf-8", "ignore")) > MAX_BYTES:
        raise ValueError(f"The file is larger than {MAX_BYTES // 1_000_000} MB. Split it or use a longer candle.")
    try:
        frame = pd.read_csv(io.StringIO(text), sep=None, engine="python")
    except Exception as exc:  # noqa: BLE001 - any parser failure is the file's problem
        raise ValueError(f"Could not read the file as CSV: {str(exc)[:160]}") from exc
    names = _columns(frame)
    missing = [c for c in ("open", "high", "low", "close") if c not in names]
    if missing:
        raise ValueError("The file needs columns " + ", ".join(c.capitalize() for c in missing) +
                         f". It has: {', '.join(map(str, frame.columns))[:200]}.")
    stamps, how = _when(frame, names)
    out = pd.DataFrame({c: pd.to_numeric(frame[names[c.lower()]], errors="coerce") if c.lower() in names else 0.0
                        for c in COLUMNS})
    out["Volume"] = out["Volume"].fillna(0.0)
    o, h, low, c = (out[k] for k in ("Open", "High", "Low", "Close"))
    good = stamps.notna() & (o > 0) & (h > 0) & (low > 0) & (c > 0) & (h >= low) & \
        (h + 1e-9 >= o.combine(c, max)) & (low - 1e-9 <= o.combine(c, min)) & (out["Volume"] >= 0)
    bad_lines = [int(i) + 2 for i in out.index[~good]]          # +1 header, +1 counting from 1
    if len(out) and len(bad_lines) > len(out) / 10:
        raise ValueError(f"{len(bad_lines)} of {len(out)} rows have a missing date or an impossible price (high below "
                         f"low, a price of zero, ...). First bad lines: {', '.join(map(str, bad_lines[:5]))}.")
    out = out[good].set_axis(pd.DatetimeIndex(stamps[good]))
    duplicates = int(out.index.duplicated(keep="last").sum())
    out = out[~out.index.duplicated(keep="last")].sort_index()
    if len(out) < 2:
        raise ValueError("The file has fewer than two usable rows.")
    step = pd.Series(out.index).diff().dropna().median()
    base = 86400 if step >= pd.Timedelta(hours=20) else max(60, int(round(step.total_seconds() / 60)) * 60)
    if base == 86400:
        out.index = out.index.normalize()
    report = {"rows": len(out), "dropped": len(bad_lines), "dropped_lines": bad_lines[:5], "duplicates": duplicates,
              "base_seconds": base, "dates": how, "first": str(out.index[0]), "last": str(out.index[-1])}
    return out, report


def _label(seconds: int) -> str:
    return "1 day" if seconds == 86400 else f"{seconds // 60} minute{'s' if seconds != 60 else ''}"


class Imports:
    def __init__(self, folder: Path):
        self.folder = Path(folder)

    def _paths(self, symbol: str) -> tuple[Path, Path]:
        return self.folder / f"{symbol}.csv", self.folder / f"{symbol}.json"

    @staticmethod
    def name(raw: str) -> str:
        symbol = (raw or "").strip().upper()
        if not NAME.fullmatch(symbol):
            raise ValueError("Name the import with letters and digits, for example RELIANCE (30 characters at most).")
        return symbol

    def list(self) -> list[dict]:
        out = []
        for meta in sorted(self.folder.glob("*.json")):
            try:
                out.append(json.loads(meta.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
        return out

    def save(self, raw_symbol: str, text: str) -> dict:
        symbol = self.name(raw_symbol)
        frame, report = parse(text)
        csv_path, meta_path = self._paths(symbol)
        if not csv_path.exists() and len(list(self.folder.glob("*.json"))) >= MAX_FILES:
            raise ValueError(f"You have {MAX_FILES} imports. Delete one first.")
        self.folder.mkdir(parents=True, exist_ok=True)
        frame.to_csv(csv_path, index_label="Datetime")
        meta = {"symbol": symbol, "imported": time.time(), "candle": _label(report["base_seconds"]), **report}
        meta_path.write_text(json.dumps(meta), encoding="utf-8")
        return meta

    def delete(self, raw_symbol: str) -> None:
        for path in self._paths(self.name(raw_symbol)):
            path.unlink(missing_ok=True)

    def candles(self, raw_symbol: str, timeframe: str) -> dict:
        check_timeframe(timeframe)
        symbol = self.name(raw_symbol)
        csv_path, meta_path = self._paths(symbol)
        if not csv_path.exists():
            raise MarketDataError(f"No imported file named {symbol}. Import one with Import CSV.", "Imported file")
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        base, want = meta["base_seconds"], TIMEFRAMES[timeframe]["seconds"]
        if want < base or (want < 86400 and want % base):
            usable = [k for k, v in TIMEFRAMES.items() if v["seconds"] >= base and (v["seconds"] >= 86400 or not v["seconds"] % base)]
            raise MarketDataError(f"{symbol} was imported with {meta['candle']} candles, so it cannot show "
                                  f"{TIMEFRAMES[timeframe]['label']} candles. Pick {', '.join(usable)}.", "Imported file")
        frame = pd.read_csv(csv_path, index_col=0, parse_dates=True)
        if timeframe == "1w":
            frame = weekly(_daily(frame) if base < 86400 else frame)
        elif timeframe == "1d" and base < 86400:
            frame = _daily(frame)
        elif want > base:
            frame = data.resample(frame, want // 60)
        rows = rows_from_frame(frame)
        if not rows:
            raise MarketDataError(f"{symbol}'s file has no usable candles.", "Imported file")
        anchor = {"1d": 0, "1w": 4 * 86400}.get(timeframe, 9 * 3600 + 15 * 60)
        note = (f"From your imported file ({meta['rows']:,} {meta['candle']} candles, {meta['first'][:16]} to "
                f"{meta['last'][:16]}). Not live.")
        return {"candles": rows[-MAX_BARS:], "source": "Imported file", "notes": [note], "session_offset": 0,
                "seconds": want, "anchor": anchor, "live": {"kind": "none"}, "name": f"{symbol} (imported)"}


def _daily(frame: pd.DataFrame) -> pd.DataFrame:
    how = {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}
    return frame[list(how)].groupby(frame.index.normalize()).agg(how)
