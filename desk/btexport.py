"""A backtest result as an Excel workbook: everything the Backtest page shows, with native Excel charts.

Sheets: Summary (the result tiles, the comparison with buy and hold and the benchmark, the trust checks and the
steps/adds comparisons), Trades (every trade, with a running total), Monthly (each month and a year-by-month grid),
Equity curve (each day's value, buy and hold, the benchmark and the fall from the high), Charts (portfolio value,
fall from the high, monthly and yearly profit/loss, each trade) and Settings (every input the backtest ran with).

Works for every backtest the page draws with one result layout: stocks, bought index options, option strategies,
index futures and ETF portfolios. All numbers are written as values, not formulas (except the filtered totals),
so the charts and figures read the same in Excel, LibreOffice and Google Sheets.
"""

from __future__ import annotations

import io
import re
from datetime import date, datetime

from openpyxl import Workbook
from openpyxl.chart import AreaChart, BarChart, LineChart, Reference
from openpyxl.chart.axis import DateAxis
from openpyxl.formatting.rule import CellIsRule, ColorScaleRule
from openpyxl.styles import Alignment, Border, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from . import data
from .xlsxexport import (
    AMBER_FILL,
    AMBER_TEXT,
    DATE,
    GREEN_FILL,
    GREEN_TEXT,
    HEADER_ROW,
    INT,
    NAVY,
    PCT,
    RATIO,
    RED_FILL,
    RED_TEXT,
    RUPEE,
    THIN,
    WHITE,
    Col,
    _sheet,
    _stamp,
)

MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
GREY = "5B6675"
LINE_COLORS = ("2F6FDE", "E08A1E", "7A52C7")       # strategy, buy and hold, benchmark
PROFIT_BAR, LOSS_BAR = "2E9E4F", "D64541"
MAX_ROWS = 1_000_000                               # Excel's sheet limit is 1,048,576 rows

EXIT_WORDS = {"signal": "Strategy's exit signal", "stop": "Stoploss", "stoploss": "Stoploss", "stop_loss": "Stoploss",
              "target": "Target", "trail": "Trailing stoploss", "trailing": "Trailing stoploss", "expiry": "Expiry",
              "square_off": "Sell-everything time", "squareoff": "Sell-everything time", "open": "Still open at the end",
              "end": "End of the period", "max_hold": "Holding limit", "hold": "Holding limit", "cost": "Stoploss at cost",
              "loss_limit": "Maximum loss", "profit_limit": "Maximum profit", "leg_stop": "Leg stoploss",
              "rebalance": "Rebalance"}

SETTING_WORDS = {
    "capital": ("Capital", "money"), "allocation_pct": ("Money used per trade", "pct"),
    "stop_loss_pct": ("Stoploss", "pct"), "target_pct": ("Target", "pct"), "trailing_stop_pct": ("Trailing stoploss", "pct"),
    "slippage_bps": ("Slippage (basis points)", "num"), "slippage_pct": ("Slippage", "pct"),
    "risk_free_rate_pct": ("Safe rate (risk-free) a year", "pct"), "exchange": ("Exchange", "text"),
    "intraday": ("Intraday", "yes"), "positional": ("Positional", "yes"), "max_hold_days": ("Holding limit (days, 0 = none)", "num"),
    "trade_start": ("Buy from", "text"), "trade_end": ("Buy until", "text"), "square_off": ("Sell everything at", "text"),
    "trade_type": ("Trade type", "text"), "direction": ("Direction", "text"), "lots": ("Lots", "num"),
    "lot_size": ("Lot size", "num"), "margin_pct": ("Margin", "pct"), "stop_pct": ("Stoploss on the whole trade", "pct"),
    "stop_rs": ("Stoploss on the whole trade (₹)", "money"), "target_rs": ("Target on the whole trade (₹)", "money"),
    "carry_pct": ("Futures carry a year", "pct"), "expiry_kind": ("Expiry", "text"),
    "expiry_ahead": ("Expiries ahead (0 = nearest)", "num"), "leg_sl_pct": ("Stoploss on each sold leg", "pct"),
    "iv_scale": ("Volatility multiplier", "num"), "leg_scope": ("Leg exits apply to", "text"),
    "rebalance": ("Rebalance", "text"), "k": ("Strikes from the money (sold)", "num"), "w": ("Wing width (strikes)", "num"),
    "unit": ("In", "text"), "stop_loss": ("Stoploss", "num"), "target": ("Target", "num"), "trail_every": ("Trail every", "num"),
    "trail_by": ("Trail by", "num"), "cost_after": ("Stoploss to cost after", "num"), "reentry": ("Re-entries after a stoploss", "num"),
    "reentry_mode": ("Re-enter", "text"), "stop_after": ("Stoploss after a step", "text"),
}


# -- small helpers ---------------------------------------------------------------------------------------

def _f(value) -> float | None:
    try:
        return None if value is None or isinstance(value, bool) else float(value)
    except (TypeError, ValueError):
        return None


def _day(text) -> date | datetime | str | None:
    """'2024-07-19', '2024-07-19 09:20' or ISO: a date, or a datetime when it has a time of day."""
    text = str(text or "").strip()
    if not text:
        return None
    try:
        stamp = datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return text
    return stamp.date() if len(text) <= 10 or (stamp.hour, stamp.minute, stamp.second) == (0, 0, 0) else stamp


def _safe_text(value) -> str:
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", str(value))


def _put(ws: Worksheet, row: int, col: int, value, fmt: str | None = None, **font):
    if isinstance(value, str):
        value = _safe_text(value)
    cell = ws.cell(row, col, value)
    if isinstance(value, str):
        cell.data_type = "s"                       # never a formula, whatever the text starts with
    if fmt:
        cell.number_format = fmt
    if font:
        cell.font = Font(**font)
    return cell


def _title(ws: Worksheet, title: str, subtitle: str) -> None:
    _put(ws, 1, 1, title, bold=True, size=14, color=NAVY)
    _put(ws, 2, 1, subtitle, italic=True, size=10, color=GREY)
    ws.sheet_view.showGridLines = False


def _head(ws: Worksheet, row: int, labels: list[str], first_col: int = 1) -> None:
    for i, label in enumerate(labels, first_col):
        cell = _put(ws, row, i, label, bold=True, color=WHITE)
        cell.fill = PatternFill("solid", fgColor=NAVY)
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)


def _section(ws: Worksheet, row: int, text: str) -> None:
    _put(ws, row, 1, text, bold=True, size=12, color=NAVY)


def _tone(ws: Worksheet, ref: str) -> None:
    ws.conditional_formatting.add(ref, CellIsRule(operator="greaterThan", formula=["0"], stopIfTrue=True,
                                                  fill=PatternFill("solid", fgColor=GREEN_FILL), font=Font(color=GREEN_TEXT)))
    ws.conditional_formatting.add(ref, CellIsRule(operator="lessThan", formula=["0"], stopIfTrue=True,
                                                  fill=PatternFill("solid", fgColor=RED_FILL), font=Font(color=RED_TEXT)))


def _points(points) -> list[tuple[str, float]]:
    """[[date, value], ...] with one value per date (the last), in date order."""
    seen: dict[str, float] = {}
    for p in points or []:
        if not isinstance(p, (list, tuple)) or len(p) < 2:
            continue
        v = _f(p[1])
        if v is not None:
            seen[str(p[0])] = v
    return sorted(seen.items())


def _kind(r: dict) -> str:
    if r.get("portfolio"):
        return "ETF portfolio"
    if r.get("structure"):
        return "Option strategy"
    if r.get("futures"):
        return "Index futures"
    if r.get("simulated"):
        return "Buy index options"
    return "Stocks"


def _interval(r: dict) -> str:
    key = r.get("interval") or ""
    return data.INTERVALS.get(key, {}).get("label", key) + " candles" if key else ""


def _trade_type(r: dict) -> str:
    kind = r.get("trade_type") or r.get("mode") or ("intraday" if (r.get("params") or {}).get("intraday") else "")
    return {"intraday": "Intraday", "positional": "Positional"}.get(str(kind), str(kind).title())


def _ratio(v) -> str:
    return "–" if v is None else f"{v:.2f}"


def filename(r: dict, app_name: str) -> str:
    symbol = str(r.get("symbol") or "backtest").removesuffix(".NS").replace("^", "")
    name = str((r.get("strategy") or {}).get("name") or "")
    period = r.get("period") or {}
    raw = f"{app_name} backtest {symbol} {name} {period.get('start', '')} to {period.get('end', '')}"
    slug = re.sub(r"[^A-Za-z0-9]+", "-", raw).strip("-").lower()
    return (slug[:120] or "backtest") + ".xlsx"


# -- monthly and drawdown --------------------------------------------------------------------------------

def _drawdowns(curve: list[tuple[str, float]]) -> list[float]:
    peak, out = None, []
    for _, v in curve:
        peak = v if peak is None or v > peak else peak
        out.append(round((v / peak - 1) * 100, 2) if peak and peak > 0 else 0.0)
    return out


def _months(curve: list[tuple[str, float]], start_value: float | None) -> list[dict]:
    """Each calendar month: value at the start and the end, profit/loss in rupees and %, and the worst fall inside."""
    ends: dict[str, float] = {}
    lows: dict[str, float] = {}
    order: list[str] = []
    for d, v in curve:
        key = d[:7]
        if key not in ends:
            order.append(key)
        ends[key] = v
        lows[key] = min(lows.get(key, v), v)
    rows, prev = [], start_value if start_value else (curve[0][1] if curve else None)
    for key in order:
        end = ends[key]
        pnl = end - prev if prev is not None else None
        ret = (end / prev - 1) * 100 if prev else None
        rows.append({"month": key, "year": int(key[:4]), "m": int(key[5:7]), "start": prev, "end": end,
                     "pnl": round(pnl, 2) if pnl is not None else None, "ret": round(ret, 2) if ret is not None else None,
                     "low": lows[key]})
        prev = end
    return rows


def _years(months: list[dict]) -> list[dict]:
    out: dict[int, dict] = {}
    for m in months:
        y = out.setdefault(m["year"], {"year": m["year"], "start": m["start"], "end": m["end"], "pnl": 0.0,
                                       "up": 0, "down": 0})
        y["end"] = m["end"]
        y["pnl"] += m["pnl"] or 0.0
        if (m["pnl"] or 0) > 0:
            y["up"] += 1
        elif (m["pnl"] or 0) < 0:
            y["down"] += 1
    for y in out.values():
        y["ret"] = round((y["end"] / y["start"] - 1) * 100, 2) if y["start"] else None
        y["pnl"] = round(y["pnl"], 2)
    return sorted(out.values(), key=lambda y: y["year"])


# -- sheets ----------------------------------------------------------------------------------------------

def _summary(ws: Worksheet, r: dict, head: str, checks: list[dict], months: list[dict]) -> None:
    m, bh, bench = r.get("metrics") or {}, r.get("buy_hold") or {}, r.get("benchmark")
    params = r.get("params") or {}
    strategy = (r.get("strategy") or {}).get("name") or "Strategy"
    _title(ws, f"Backtest: {strategy}", head)
    ws.column_dimensions["A"].width = 42
    for letter in "BCD":
        ws.column_dimensions[letter].width = 22
    ws.column_dimensions["E"].width = 60

    row = HEADER_ROW
    _head(ws, row, ["Result", "Value", "Compared with"])
    trades, wins = m.get("trades"), m.get("win_rate_pct")
    best = max((m_["pnl"] for m_ in months if m_["pnl"] is not None), default=None)
    worst = min((m_["pnl"] for m_ in months if m_["pnl"] is not None), default=None)
    up = sum(1 for m_ in months if (m_["pnl"] or 0) > 0)
    tiles = [
        ("Total return", _f(m.get("total_return_pct")), PCT, f"Buy & hold {bh.get('total_return_pct', '–')}%", True),
        ("CAGR (return a year)", _f(m.get("cagr_pct")), PCT,
         f"{r.get('benchmark_name')} {bench.get('cagr_pct')}%" if bench else "", True),
        ("Max drawdown (worst fall from a high)", _f(m.get("max_drawdown_pct")), PCT, f"on {m.get('max_drawdown_date', '–')}", False),
        ("Sharpe ratio", _f(m.get("sharpe")), RATIO, f"Sortino {_ratio(_f(m.get('sortino')))}", False),
        ("Volatility a year", _f(m.get("volatility_pct")), PCT, "", False),
        ("Win rate", _f(wins), PCT, f"{trades} closed trade{'' if trades == 1 else 's'}" if trades is not None else "", False),
        ("Profit factor", _f(m.get("profit_factor")), RATIO, "Total profit ÷ total loss", False),
        ("Average win", _f(m.get("avg_win")), RUPEE, "", True),
        ("Average loss", _f(m.get("avg_loss")), RUPEE, "", True),
        ("Average days held", _f(m.get("avg_days_held")), "0.0", "", False),
        ("In the market", _f(m.get("exposure_pct")), PCT, "of days", False),
        ("Starting capital", _f(params.get("capital")), RUPEE, "", False),
        ("Final value", _f(m.get("final_value")), RUPEE, f"Profit/Loss ₹{(_f(m.get('final_value')) or 0) - (_f(params.get('capital')) or 0):,.2f}", False),
        ("Net profit/loss", (_f(m.get("final_value")) or 0) - (_f(params.get("capital")) or 0), RUPEE, "after charges", True),
        ("Charges paid", _f(m.get("total_charges")), RUPEE, "STT, stamp, GST, exchange, DP", False),
        ("Months in profit", up, INT, f"of {len(months)} months", False),
        ("Best month", best, RUPEE, "", True),
        ("Worst month", worst, RUPEE, "", True),
    ]
    if m.get("open_trades"):
        tiles.append(("Trades still open at the end", m.get("open_trades"), INT, "valued at the last price", False))
    if m.get("reentries"):
        tiles.append(("Re-entries", m.get("reentries"), INT, "after a stoploss", False))
    if m.get("steps_booked"):
        tiles.append(("Parts booked in steps", m.get("steps_booked"), INT, "", False))
    if m.get("adds_made") or m.get("adds_skipped"):
        tiles.append(("Adds to winners", m.get("adds_made"), INT, f"{m.get('adds_skipped') or 0} skipped for want of money", False))
    if r.get("futures"):
        tiles += [("Long trades", m.get("long_trades"), INT, "", False), ("Short trades", m.get("short_trades"), INT, "", False),
                  ("Rolls to the next month", m.get("rolls"), INT, "", False),
                  ("Margin a lot", _f(m.get("margin_per_lot")), RUPEE, "", False)]
    if r.get("structure"):
        tiles += [("Legs stopped out", m.get("legs_stopped"), INT, "", False),
                  ("Average margin", _f(m.get("avg_margin")), RUPEE, "", False)]
    if m.get("skipped_entries") or r.get("skipped_entries"):
        tiles.append(("Entries skipped", m.get("skipped_entries") or r.get("skipped_entries"), INT,
                      "not enough money for a lot", False))
    src = r.get("data_source")
    if src:
        tiles.append(("Prices from", src.get("name"), None, f"{src.get('first')} to {src.get('last')}", False))
    for label, value, fmt, note, toned in tiles:
        row += 1
        _put(ws, row, 1, label, bold=True).alignment = Alignment(indent=1)
        cell = _put(ws, row, 2, "–" if value is None else value, fmt if value is not None else None)
        cell.alignment = Alignment(horizontal="right")
        _put(ws, row, 3, note, color=GREY, italic=True)
        for c in (1, 2, 3):
            ws.cell(row, c).border = Border(bottom=THIN)
        if toned and isinstance(value, (int, float)):
            _tone(ws, f"B{row}")

    # Strategy against buy and hold and the benchmark: the page's "Compare with benchmarks".
    row += 2
    _section(ws, row, "Compare with buy and hold and the benchmark")
    row += 1
    cols = [(strategy, m), (r.get("buy_hold_name") or "Buy & hold", bh)]
    if bench:
        cols.append((r.get("benchmark_name") or "Benchmark", bench))
    _head(ws, row, [""] + [c[0] for c in cols])
    for label, key, fmt in (("Total return", "total_return_pct", PCT), ("CAGR", "cagr_pct", PCT),
                            ("Volatility", "volatility_pct", PCT), ("Sharpe ratio", "sharpe", RATIO),
                            ("Max drawdown", "max_drawdown_pct", PCT), ("Final value", "final_value", RUPEE)):
        row += 1
        _put(ws, row, 1, label, bold=True).alignment = Alignment(indent=1)
        for i, (_, stats) in enumerate(cols, 2):
            v = _f((stats or {}).get(key))
            _put(ws, row, i, "–" if v is None else v, fmt if v is not None else None).alignment = Alignment(horizontal="right")
            ws.cell(row, i).border = Border(bottom=THIN)
        ws.cell(row, 1).border = Border(bottom=THIN)
    if r.get("benchmark_note"):
        row += 1
        _put(ws, row, 1, r["benchmark_note"], italic=True, color=GREY)

    for key, title, a_name, b_name, a_label, b_label in (
            ("steps_compare", "Book profit in steps, against one exit", "with_steps", "single_exit", "With steps", "One exit"),
            ("adds_compare", "Add to winners, against keeping the first size", "with_adds", "without_adds", "With adds", "Without adds")):
        c = r.get(key)
        if not c:
            continue
        row += 2
        _section(ws, row, title)
        row += 1
        _head(ws, row, ["", a_label, b_label])
        for label, k, fmt in (("Total return", "total_return_pct", PCT), ("Final value", "final_value", RUPEE),
                              ("Max drawdown", "max_drawdown_pct", PCT), ("Win rate", "win_rate_pct", PCT),
                              ("Average win", "avg_win", RUPEE), ("Average loss", "avg_loss", RUPEE),
                              ("Profit factor", "profit_factor", RATIO), ("Closed trades", "trades", INT)):
            row += 1
            _put(ws, row, 1, label, bold=True).alignment = Alignment(indent=1)
            for i, side in ((2, a_name), (3, b_name)):
                v = _f((c.get(side) or {}).get(k))
                _put(ws, row, i, "–" if v is None else v, fmt if v is not None else None).alignment = Alignment(horizontal="right")
        row += 1
        diff = _f(c.get("difference"))
        if diff is not None:
            _put(ws, row, 1, "Difference (first against second)", bold=True).alignment = Alignment(indent=1)
            _put(ws, row, 2, diff, RUPEE).alignment = Alignment(horizontal="right")
            _tone(ws, f"B{row}")

    # Can you trust this result? The same sentences as the page.
    if checks:
        row += 2
        _section(ws, row, "Can you trust this result?")
        for item in checks:
            row += 1
            tone = str(item.get("tone") or "")
            mark = {"ok": "✓", "warn": "!"}.get(tone, "•")
            _put(ws, row, 1, mark, bold=True, color={"ok": GREEN_TEXT, "warn": AMBER_TEXT}.get(tone, GREY)).alignment = \
                Alignment(horizontal="right", vertical="top")
            cell = _put(ws, row, 2, str(item.get("text") or ""))
            cell.alignment = Alignment(wrap_text=True, vertical="top")
            ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=5)
            if tone == "warn":
                cell.fill = PatternFill("solid", fgColor=AMBER_FILL)
            ws.row_dimensions[row].height = 15 * max(1, len(cell.value) // 120 + 1)
    ws.freeze_panes = None


def _trade_cols(r: dict, trades: list[dict]) -> list[Col]:
    any_ = lambda key: any(t.get(key) not in (None, "", 0, False, []) for t in trades)  # noqa: E731
    cols = [Col("n", "No.", "int", 6)]
    if r.get("simulated") or r.get("structure"):
        cols.append(Col("contract", "Contract", width=34))
    if r.get("futures"):
        cols.append(Col("side", "Side", width=8, get=lambda t: {"LONG": "Long", "SHORT": "Short"}.get(str(t.get("side")), t.get("side")),
                        tone="side"))
    cols += [Col("entry", "Entry", "date", 17, get=lambda t: t.get("entry_date")),
             Col("exit", "Exit", "date", 17, get=lambda t: t.get("exit_date") or None)]
    if any_("reentry"):
        cols.append(Col("reentry", "Re-entry", width=9, get=lambda t: "Yes" if t.get("reentry") else ""))
    if any_("lots"):
        cols.append(Col("lots", "Lots", "int", 7))
    cols.append(Col("quantity", "Qty", "qty", 10))
    structure = bool(r.get("structure"))
    cols += [Col("entry_price", "Net credit (+) / debit (−) per unit" if structure else "Entry ₹", "price", 14),
             Col("exit_price", "Index at exit" if structure else "Exit ₹", "price", 14),
             Col("pnl", "Net Profit/Loss", "money", 16, True, tone="pnl"),
             Col("return_pct", "Return", "pct", 10, tone="pnl"),
             Col("cum", "Running total", "money", 16, tone="pnl")]
    if any_("worst_pct"):
        cols += [Col("worst_pct", "Lowest while held", "pct", 11), Col("best_pct", "Highest while held", "pct", 11)]
    cols.append(Col("days", "Days", "int", 7))
    cols.append(Col("exit_reason", "Exit reason", width=22,
                    get=lambda t: EXIT_WORDS.get(str(t.get("exit") or ""), str(t.get("exit") or "").replace("_", " ").capitalize())))
    if structure:
        cols += [Col("legs_stopped", "Legs stopped out", "int", 9), Col("reentries", "Re-entries", "int", 9),
                 Col("margin", "Margin", "money", 14)]
    if r.get("futures"):
        cols.append(Col("rolls", "Rolls", "int", 7))
    if any_("partials"):
        cols.append(Col("steps", "Booked in steps", width=44, get=lambda t: _parts(t.get("partials"), steps=True)))
    if any_("adds"):
        cols += [Col("adds_text", "Added", width=44, get=lambda t: _parts(t.get("adds"), steps=False)),
                 Col("average_price", "Average entry ₹", "price", 14)]
    cols.append(Col("status", "Status", width=10, get=lambda t: "Open" if t.get("open") else "Closed"))
    return cols


def _parts(parts, *, steps: bool) -> str:
    out = []
    for p in parts or []:
        size = f"{p['lots']} lot{'' if p['lots'] == 1 else 's'}" if p.get("lots") else f"{_f(p.get('quantity')) or 0:,.0f} shares"
        text = f"{p.get('date') or p.get('time') or ''} {size}"
        if p.get("leg"):
            text += f" of {p['leg']}"
        if p.get("price") is not None:
            text += f" @ {_f(p['price']):,.2f}"
        if steps and p.get("pnl") is not None:
            text += f" (₹{_f(p['pnl']):,.2f})"
        if not steps and "skipped" in str(p.get("why") or ""):
            text += f" ({re.sub(r'^add [0-9]+ ', '', str(p['why']))})"
        out.append(text.strip())
    return "; ".join(out)


def _trades(ws: Worksheet, r: dict, head: str) -> None:
    trades = [dict(t) for t in (r.get("trades") or [])][:MAX_ROWS]
    total = 0.0
    for n, t in enumerate(trades, 1):
        total += _f(t.get("pnl")) or 0.0
        t["n"], t["cum"] = n, round(total, 2)
        for key in ("entry_date", "exit_date"):
            t[key] = _day(t.get(key))
    cols = _trade_cols(r, trades)
    wins = sum(1 for t in trades if (_f(t.get("pnl")) or 0) > 0)
    _sheet(ws, "Every trade", f"{head} · {len(trades)} trades, {wins} in profit · oldest first · after charges and slippage",
           cols, trades, empty="The strategy made no trades in this period.")
    for i, col in enumerate(cols, 1):            # dates with a time of day show it
        if col.kind == "date":
            for row in ws.iter_rows(min_row=HEADER_ROW + 1, max_row=HEADER_ROW + len(trades), min_col=i, max_col=i):
                if isinstance(row[0].value, datetime):
                    row[0].number_format = "dd-mmm-yyyy hh:mm"


def _assets(ws: Worksheet, r: dict, head: str) -> None:
    m = r.get("metrics") or {}
    rows = [{"symbol": str(a.get("symbol", "")).removesuffix(".NS"), "weight": a.get("weight")} for a in r.get("assets") or []]
    _sheet(ws, "ETFs and weights", f"{head} · {m.get('trades', 0)} rebalances",
           [Col("symbol", "ETF", width=18), Col("weight", "Weight", "pct", 10, True)], rows)


def _equity(ws: Worksheet, r: dict, head: str, curve, bh, bench) -> tuple[int, int]:
    """Date, strategy, buy and hold, benchmark and the fall from the high. Returns (first, last) data rows."""
    strategy = (r.get("strategy") or {}).get("name") or "Strategy"
    bh_map, bench_map = dict(bh), dict(bench)
    labels = ["Date", strategy, r.get("buy_hold_name") or "Buy & hold"]
    if bench:
        labels.append(r.get("benchmark_name") or "Benchmark")
    labels += ["Fall from the high", "Day's change"]
    _title(ws, "Portfolio value, each day", f"{head} · {len(curve)} days")
    _head(ws, HEADER_ROW, labels)
    ws.row_dimensions[HEADER_ROW].height = 30
    widths = [14, 18, 18] + ([18] if bench else []) + [14, 14]
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    falls = _drawdowns(curve)
    prev = None
    first = HEADER_ROW + 1
    dd_col = len(labels) - 1
    for i, ((d, v), fall) in enumerate(zip(curve[:MAX_ROWS], falls[:MAX_ROWS], strict=True), first):
        _put(ws, i, 1, _day(d), DATE)
        _put(ws, i, 2, v, RUPEE)
        b = bh_map.get(d)
        _put(ws, i, 3, b, RUPEE if b is not None else None)
        if bench:
            k = bench_map.get(d)
            _put(ws, i, 4, k, RUPEE if k is not None else None)
        _put(ws, i, dd_col, fall, PCT)
        _put(ws, i, dd_col + 1, round(v - prev, 2) if prev is not None else None, RUPEE)
        prev = v
    last = first + min(len(curve), MAX_ROWS) - 1
    ws.freeze_panes = ws.cell(first, 2)
    if curve:
        ws.auto_filter.ref = f"A{HEADER_ROW}:{get_column_letter(len(labels))}{last}"
        letter = get_column_letter(dd_col + 1)
        _tone(ws, f"{letter}{first}:{letter}{last}")
        dl = get_column_letter(dd_col)
        ws.conditional_formatting.add(f"{dl}{first}:{dl}{last}", CellIsRule(operator="lessThan", formula=["0"],
                                                                              font=Font(color=RED_TEXT)))
    return first, last


def _monthly(ws: Worksheet, head: str, months: list[dict]) -> dict:
    """Each month as a row. Returns the data rows for the charts."""
    _title(ws, "Profit/Loss each month", f"{head} · from the portfolio value at each month's end, after charges")
    for k, w in {"A": 12, "B": 18, "C": 18, "D": 18, "E": 11}.items():
        ws.column_dimensions[k].width = w
    _head(ws, HEADER_ROW, ["Month", "Value at start", "Value at end", "Profit/Loss", "Return"])
    ws.row_dimensions[HEADER_ROW].height = 30
    first = HEADER_ROW + 1
    for i, m in enumerate(months, first):
        _put(ws, i, 1, date(m["year"], m["m"], 1), "mmm-yyyy").alignment = Alignment(horizontal="left")
        _put(ws, i, 2, m["start"], RUPEE)
        _put(ws, i, 3, m["end"], RUPEE)
        _put(ws, i, 4, m["pnl"], RUPEE)
        _put(ws, i, 5, m["ret"], PCT)
        # The profit and loss columns the bar chart reads: green bars up, red bars down.
        _put(ws, i, 7, m["pnl"] if (m["pnl"] or 0) > 0 else None, RUPEE)
        _put(ws, i, 8, m["pnl"] if (m["pnl"] or 0) < 0 else None, RUPEE)
    last = first + len(months) - 1
    _put(ws, HEADER_ROW, 7, "Profit months")
    _put(ws, HEADER_ROW, 8, "Loss months")
    ws.column_dimensions["G"].hidden = ws.column_dimensions["H"].hidden = True
    ws.freeze_panes = ws.cell(first, 1)
    if months:
        _tone(ws, f"D{first}:E{last}")
        ws.auto_filter.ref = f"A{HEADER_ROW}:E{last}"
        total = last + 2
        _put(ws, total, 1, "Total (rows shown)", bold=True, color=NAVY)
        cell = _put(ws, total, 4, f"=SUBTOTAL(109,D{first}:D{last})", RUPEE, bold=True)
        cell.data_type = "f"
        _tone(ws, f"D{total}")
    return {"first": first, "last": last}


def _yearly(ws: Worksheet, head: str, months: list[dict], years: list[dict]) -> dict:
    """One row a year: the return of each month (coloured red to green), the year's return and profit/loss."""
    _title(ws, "Return each month and year (%)", f"{head} · each month against the value at the previous month's end")
    _head(ws, HEADER_ROW, ["Year", *MONTHS, "Whole year", "Profit/Loss", "Months up", "Months down"])
    ws.row_dimensions[HEADER_ROW].height = 30
    ws.column_dimensions["A"].width = 8
    for j in range(2, 14):
        ws.column_dimensions[get_column_letter(j)].width = 8.5
    for letter, w in (("N", 11), ("O", 16), ("P", 9), ("Q", 9)):
        ws.column_dimensions[letter].width = w
    by = {(m["year"], m["m"]): m["ret"] for m in months}
    first = HEADER_ROW + 1
    for i, y in enumerate(years, first):
        _put(ws, i, 1, y["year"], "0", bold=True)
        for mm in range(1, 13):
            v = by.get((y["year"], mm))
            if v is not None:
                _put(ws, i, mm + 1, v, PCT).alignment = Alignment(horizontal="center")
        _put(ws, i, 14, y["ret"], PCT, bold=True).alignment = Alignment(horizontal="center")
        _put(ws, i, 15, y["pnl"], RUPEE)
        _put(ws, i, 16, y["up"], INT)
        _put(ws, i, 17, y["down"], INT)
        for c in range(1, 18):
            ws.cell(i, c).border = Border(bottom=THIN)
    last = first + len(years) - 1
    if years:
        rule = ColorScaleRule(start_type="min", start_color="F8696B", mid_type="num", mid_value=0, mid_color="FFFFFF",
                              end_type="max", end_color="63BE7B")
        ws.conditional_formatting.add(f"B{first}:M{last}", rule)
        _tone(ws, f"N{first}:O{last}")
    ws.freeze_panes = ws.cell(first, 2)
    return {"first": first, "last": last}


def _line(title: str, y_title: str, fmt: str, height: float = 9.5, width: float = 26) -> LineChart:
    chart = LineChart()
    chart.title, chart.height, chart.width = title, height, width
    chart.y_axis.title, chart.y_axis.number_format = y_title, fmt
    chart.legend.position = "b"
    return chart


def _date_axis(chart, dates: Reference, days: int) -> None:
    chart.x_axis = DateAxis(crossAx=100)
    chart.y_axis.crossAx = 500                                       # the DateAxis's id: the axes must name each other
    chart.x_axis.number_format = "mmm-yy"
    chart.x_axis.majorTimeUnit = "months"
    chart.x_axis.majorUnit = max(1, round(days / 30.4 / 12))       # about a dozen labels, whatever the period
    chart.x_axis.tickLblPos = "low"                                  # under the chart, also when values go below 0
    chart.x_axis.title = None
    chart.set_categories(dates)
    chart.x_axis.delete = False
    chart.y_axis.delete = False


def _charts(ws: Worksheet, r: dict, head: str, eq: Worksheet, eq_rows: tuple[int, int], bench: bool,
            mo: Worksheet, mo_rows: dict, yr: Worksheet, yr_rows: dict, tr: Worksheet | None, n_trades: int, pnl_col: int | None) -> None:
    _title(ws, "Charts", head + " · drawn from the Equity curve, Monthly, Yearly and Trades sheets")
    first, last = eq_rows
    span = 0
    if last >= first:
        a, b = eq.cell(first, 1).value, eq.cell(last, 1).value
        span = (b - a).days if isinstance(a, date) and isinstance(b, date) else last - first
    anchor = 4
    if last >= first:
        dates = Reference(eq, min_col=1, min_row=first, max_row=last)
        chart = _line("Portfolio value", "₹", '"₹"#,##0')
        n_series = 3 + (1 if bench else 0)
        chart.add_data(Reference(eq, min_col=2, max_col=n_series, min_row=HEADER_ROW, max_row=last), titles_from_data=True)
        for s, color in zip(chart.series, LINE_COLORS, strict=False):
            s.graphicalProperties.line.solidFill = color
            s.graphicalProperties.line.width = 19050 if color == LINE_COLORS[0] else 12700
            s.smooth = False
        values = [eq.cell(i, c).value for i in range(first, last + 1) for c in range(2, n_series + 1)]
        values = [v for v in values if isinstance(v, (int, float))]
        if values:                                    # not from zero: the moves would be a flat line
            lo, hi = min(values), max(values)
            step = 10 ** max(0, len(str(int(max(hi - lo, 1)))) - 2)
            chart.y_axis.scaling.min = max(0, (lo - (hi - lo) * 0.05) // step * step)
        _date_axis(chart, dates, span)
        ws.add_chart(chart, f"A{anchor}")
        anchor += 20

        dd_col = n_series + 1
        area = AreaChart()
        area.title, area.height, area.width = "Fall from the high (drawdown)", 7.5, 26
        area.y_axis.title, area.y_axis.number_format = "%", '0"%"'
        area.add_data(Reference(eq, min_col=dd_col, min_row=HEADER_ROW, max_row=last), titles_from_data=True)
        area.series[0].graphicalProperties.solidFill = "F2A6A3"
        area.series[0].graphicalProperties.line.solidFill = RED_TEXT
        area.legend = None
        _date_axis(area, dates, span)
        ws.add_chart(area, f"A{anchor}")
        anchor += 16

    if mo_rows["last"] >= mo_rows["first"]:
        bars = BarChart()
        bars.type, bars.grouping, bars.overlap = "col", "clustered", 100
        bars.title, bars.height, bars.width = "Profit/Loss each month", 8.5, 26
        bars.y_axis.number_format, bars.y_axis.title = '"₹"#,##0', "₹"
        bars.add_data(Reference(mo, min_col=7, max_col=8, min_row=HEADER_ROW, max_row=mo_rows["last"]), titles_from_data=True)
        bars.set_categories(Reference(mo, min_col=1, min_row=mo_rows["first"], max_row=mo_rows["last"]))
        bars.series[0].graphicalProperties.solidFill = PROFIT_BAR
        bars.series[1].graphicalProperties.solidFill = LOSS_BAR
        bars.x_axis.number_format = "mmm-yy"
        bars.x_axis.delete = bars.y_axis.delete = False
        bars.x_axis.tickLblPos = "low"
        bars.legend.position = "b"
        bars.visible_cells_only = False
        ws.add_chart(bars, f"A{anchor}")
        anchor += 18

        years = BarChart()
        years.type = "col"
        years.title, years.height, years.width = "Return each year", 7.5, 13
        years.y_axis.number_format, years.y_axis.title = '0"%"', "%"
        years.add_data(Reference(yr, min_col=14, min_row=HEADER_ROW, max_row=yr_rows["last"]), titles_from_data=True)
        years.set_categories(Reference(yr, min_col=1, min_row=yr_rows["first"], max_row=yr_rows["last"]))
        years.series[0].graphicalProperties.solidFill = LINE_COLORS[0]
        years.legend = None
        years.x_axis.delete = years.y_axis.delete = False
        years.x_axis.tickLblPos = "low"
        ws.add_chart(years, f"A{anchor}")

        if tr is not None and n_trades and pnl_col:
            each = BarChart()
            each.type, each.grouping, each.overlap = "col", "clustered", 100
            each.title, each.height, each.width = "Profit/Loss of each trade", 7.5, 13
            each.y_axis.number_format = '"₹"#,##0'
            each.add_data(Reference(tr, min_col=pnl_col, max_col=pnl_col + 1, min_row=HEADER_ROW,
                                    max_row=HEADER_ROW + n_trades), titles_from_data=True)
            each.set_categories(Reference(tr, min_col=1, min_row=HEADER_ROW + 1, max_row=HEADER_ROW + n_trades))
            each.series[0].graphicalProperties.solidFill = PROFIT_BAR
            each.series[1].graphicalProperties.solidFill = LOSS_BAR
            each.x_axis.delete = each.y_axis.delete = False
            each.x_axis.tickLblPos = "low"
            each.legend.position = "b"
            each.visible_cells_only = False
            ws.add_chart(each, f"I{anchor}")


def _trade_bars(ws: Worksheet, trades: list[dict]) -> int | None:
    """Two hidden columns on the Trades sheet: each trade's profit or loss, for the chart's green and red bars."""
    if not trades:
        return None
    col = ws.max_column + 2
    _put(ws, HEADER_ROW, col, "Profit trades")
    _put(ws, HEADER_ROW, col + 1, "Loss trades")
    for i, t in enumerate(trades, HEADER_ROW + 1):
        v = _f(t.get("pnl")) or 0.0
        _put(ws, i, col, v if v > 0 else None, RUPEE)
        _put(ws, i, col + 1, v if v < 0 else None, RUPEE)
    ws.column_dimensions[get_column_letter(col)].hidden = True
    ws.column_dimensions[get_column_letter(col + 1)].hidden = True
    return col


def _flatten(prefix: str, value, out: list[dict]) -> None:
    if isinstance(value, dict):
        for k, v in value.items():
            _flatten(f"{prefix} · {SETTING_WORDS.get(k, (k.replace('_', ' ').capitalize(), ''))[0]}" if prefix
                     else SETTING_WORDS.get(k, (k.replace("_", " ").capitalize(), ""))[0], v, out)
        return
    if isinstance(value, list):
        if all(isinstance(v, dict) for v in value) and value:
            for i, v in enumerate(value, 1):
                _flatten(f"{prefix} {i}", v, out)
            return
        value = ", ".join(str(v) for v in value)
    if isinstance(value, bool):
        value = "Yes" if value else "No"
    out.append({"item": prefix, "value": "" if value is None else value})


def _settings(ws: Worksheet, r: dict, head: str, notes: list[str]) -> None:
    rows: list[dict] = [{"item": "Backtest", "value": _kind(r)}]
    if r.get("symbol"):
        rows.append({"item": "Symbol", "value": str(r["symbol"]).removesuffix(".NS")})
    strategy = r.get("strategy") or {}
    rows.append({"item": "Strategy", "value": strategy.get("name") or ""})
    for k, v in (strategy.get("params") or {}).items():
        rows.append({"item": f"Strategy · {k.replace('_', ' ')}", "value": v})
    if r.get("legs_text"):
        rows.append({"item": "Legs", "value": r["legs_text"]})
    if r.get("interval"):
        rows.append({"item": "Candles", "value": _interval(r)})
    if _trade_type(r):
        rows.append({"item": "Trade type", "value": _trade_type(r)})
    period = r.get("period") or {}
    if period:
        rows.append({"item": "Period", "value": f"{period.get('start')} to {period.get('end')} ({period.get('bars', '–')} candles)"})
    if r.get("lot_size"):
        rows.append({"item": "Lot size", "value": r["lot_size"]})
    if r.get("pricing"):
        _flatten("Option prices", r["pricing"], rows)
    params = {k: v for k, v in (r.get("params") or {}).items() if v not in (None, {}, [])}
    _flatten("", params, rows)
    if r.get("benchmark_name"):
        rows.append({"item": "Benchmark", "value": r["benchmark_name"]})
    src = r.get("data_source") or {}
    if src.get("note"):
        rows.append({"item": "Prices", "value": src["note"]})
    for text in [r.get("data_note"), *notes]:
        if text:
            rows.append({"item": "Note", "value": text})
    _sheet(ws, "Settings this backtest ran with", head, [Col("item", "Setting", width=44), Col("value", "Value", width=100)],
           rows, totals=False)
    for row in ws.iter_rows(min_row=HEADER_ROW + 1):
        row[1].alignment = Alignment(wrap_text=True, vertical="top", horizontal="left", indent=1)
        row[0].alignment = Alignment(vertical="top", indent=1)
        row[0].font = Font(bold=True)


def workbook(r: dict, *, checks: list[dict] | None = None, notes: list[str] | None = None,
             app_name: str = "TradeSetu") -> bytes:
    """One backtest result (the JSON the backtest APIs return) as an .xlsx file.

    ``checks`` are the page's "Can you trust this result?" lines ({"tone": "ok"|"warn"|"", "text": ...}) so the
    workbook says exactly what the page said; ``notes`` are extra explanations to keep with the settings.
    """
    if not isinstance(r, dict) or "metrics" not in r or "equity" not in r:
        raise ValueError("Run a backtest first: there is no result to export.")
    strategy = (r.get("strategy") or {}).get("name") or "Strategy"
    period = r.get("period") or {}
    bits = [app_name, _kind(r), str(r.get("symbol") or "").removesuffix(".NS"), strategy, _interval(r), _trade_type(r),
            f"{period.get('start', '')} to {period.get('end', '')}", f"made {_stamp()} IST"]
    head = " · ".join(b for b in bits if b and b.strip() != "to")

    curve = _points(r.get("equity"))
    bh, bench = _points(r.get("buy_hold_equity")), _points(r.get("benchmark_equity"))
    capital = _f((r.get("params") or {}).get("capital"))
    months = _months(curve, capital)
    years = _years(months)

    wb = Workbook()
    summary = wb.active
    summary.title = "Summary"
    _summary(summary, r, head, [c for c in (checks or []) if isinstance(c, dict)][:40], months)
    charts = wb.create_sheet("Charts")
    tr = None
    if r.get("portfolio"):
        _assets(wb.create_sheet("ETFs"), r, head)
        trades: list[dict] = []
    else:
        tr = wb.create_sheet("Trades")
        _trades(tr, r, head)
        trades = list(r.get("trades") or [])[:MAX_ROWS]
    mo = wb.create_sheet("Monthly")
    mo_rows = _monthly(mo, head, months)
    yr = wb.create_sheet("Yearly")
    yr_rows = _yearly(yr, head, months, years)
    eq = wb.create_sheet("Equity curve")
    eq_rows = _equity(eq, r, head, curve, bh, bench)
    _settings(wb.create_sheet("Settings"), r, head, [str(n) for n in (notes or []) if n][:10])
    pnl_col = _trade_bars(tr, trades) if tr is not None else None
    _charts(charts, r, head, eq, eq_rows, bool(bench), mo, mo_rows, yr, yr_rows, tr, len(trades), pnl_col)

    for ws in wb.worksheets:
        ws.page_setup.orientation = "landscape"
        ws.page_setup.fitToWidth, ws.page_setup.fitToHeight = 1, 0
        ws.sheet_properties.pageSetUpPr.fitToPage = True
    wb.properties.creator = app_name
    wb.properties.title = f"Backtest: {strategy}"
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


__all__ = ["workbook", "filename"]
