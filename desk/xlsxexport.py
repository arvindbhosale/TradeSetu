"""Colour-coded Excel (.xlsx) workbooks of the trade records: closed trades, order journal, fills and tax lots.

Each sheet has a title, a frozen header row with filters, rupee and date formats, profit in green and
loss in red, Buy/Sell in colour and a totals row that follows the filter (SUBTOTAL). Colours are
conditional formats, so they stay right after the user sorts or edits a cell.
"""

from __future__ import annotations

import io
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from openpyxl import Workbook
from openpyxl.formatting.rule import CellIsRule, FormulaRule
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from . import taxreport as tax

RUPEE = '"₹"#,##0.00;-"₹"#,##0.00;"₹"0.00'
PRICE = '#,##0.00'
QTY = '#,##0.######'
PCT = '0.00"%";-0.00"%";0.00"%"'
DATE = "dd-mmm-yyyy"
TIME = "dd-mmm-yyyy hh:mm:ss"
INT = "#,##0"

NAVY, WHITE = "1F3A5F", "FFFFFF"
GREEN_FILL, GREEN_TEXT = "E2F4E6", "1B7A34"
RED_FILL, RED_TEXT = "FBE3E3", "B3261E"
AMBER_FILL, AMBER_TEXT = "FFF1D6", "8A5A00"
VERDICT_GOOD = ("Beats buy and hold", "Works", "Yes")
VERDICT_WARN = ("Could be luck", "Fragile", "Below a fixed deposit", "Too few trades")
VERDICT_BAD = ("Loses money",)
BAND = {"LTCG": "E3EDFB", "STCG": "FFF4D6", "Speculative (intraday)": "F1E6FA", "F&O (non-speculative)": "E0F4F4"}

HEADER_ROW = 4                       # rows 1-2 title, row 3 blank, row 4 header, data from row 5
THIN = Side(style="thin", color="C9D1DC")


@dataclass(frozen=True)
class Col:
    key: str
    label: str
    kind: str = "text"               # text, time, date, money, price, qty, pct, int
    width: int = 12
    total: bool = False              # sum in the totals row
    get: Callable[[dict], object] | None = None
    tone: str | None = None          # "pnl" green/red by sign, "side" Buy green / Sell red, "verdict" scan verdicts


RATIO = "0.00"
FORMATS = {"time": TIME, "date": DATE, "money": RUPEE, "price": PRICE, "qty": QTY, "pct": PCT, "int": INT, "ratio": RATIO}


def source_label(source: str) -> str:
    """Where a trade came from, in words (the desk's own labels)."""
    source = str(source or "manual")
    if source.startswith("algo:"):
        return "Algo: " + source[5:]
    if source.startswith("fno:"):
        return "F&O algo: " + source[4:]
    return source[:1].upper() + source[1:]


def _side(row: dict) -> str:
    side = str(row.get("side") or "").upper()
    return {"BUY": "Buy", "SELL": "Sell", "LONG": "Long", "SHORT": "Short"}.get(side, side.title())


def _status(row: dict) -> str:
    if not row.get("ok"):
        return "Failed"
    if row.get("fill_price") not in (None, ""):
        return "Filled"
    return "Set" if row.get("gtt") else "Placed"           # a stoploss/target trigger order was set


def _yes(key: str) -> Callable[[dict], str]:
    return lambda row: "Yes" if row.get(key) else ""


def _src(row: dict) -> str:
    return source_label(row.get("source"))


PNL = [Col("time_ist", "Closed on (IST)", "time", 22), Col("source", "From", width=22, get=_src),
       Col("symbol", "Stock / contract", width=24), Col("side", "Side", width=8, get=_side, tone="side"),
       Col("quantity", "Quantity", "qty", 10), Col("buy_price", "Buy price", "price", 12),
       Col("sell_price", "Sell price", "price", 12), Col("charges", "Charges", "money", 12, True),
       Col("pnl", "Profit/Loss", "money", 15, True, tone="pnl"),
       Col("return_pct", "Return", "pct", 10, tone="pnl")]
JOURNAL = [Col("time_ist", "Time (IST)", "time", 22), Col("mode", "Account", width=9, get=lambda r: str(r.get("mode", "")).title()),
           Col("source", "From", width=22, get=_src), Col("exchange", "Exchange", width=9),
           Col("tradingsymbol", "Stock / contract", width=24), Col("side", "Side", width=8, get=_side, tone="side"),
           Col("quantity", "Quantity", "qty", 10), Col("product", "Product", width=9), Col("order_type", "Order type", width=11),
           Col("price", "Limit price", "price", 12), Col("fill_price", "Filled at", "price", 12),
           Col("variety", "Variety", width=9), Col("order_id", "Order ID", width=20),
           Col("ok", "Status", width=9, get=_status, tone="status"), Col("error", "Reason (if failed)", width=40),
           Col("gtt", "Stoploss/Target order", width=12, get=_yes("gtt"))]
FILLS = [Col("time_ist", "Time (IST)", "time", 22), Col("mode", "Account", width=9, get=lambda r: str(r.get("mode", "")).title()),
         Col("source", "From", width=22, get=_src), Col("exchange", "Exchange", width=9),
         Col("tradingsymbol", "Stock / contract", width=24), Col("side", "Side", width=8, get=_side, tone="side"),
         Col("quantity", "Quantity", "qty", 10), Col("price", "Price", "price", 12),
         Col("value", "Value", "money", 15, get=lambda r: _num(r.get("quantity")) * _num(r.get("price")) * _num(r.get("multiplier") or 1)),
         Col("product", "Product", width=9), Col("charges", "Charges", "money", 12, True), Col("trade_id", "Trade ID", width=20)]
LOTS = [Col("fy", "Financial year", width=10, get=lambda r: "FY " + str(r.get("fy", ""))),
        Col("category", "Tax head", width=22, tone="band"), Col("symbol", "Stock / contract", width=24),
        Col("exchange", "Exchange", width=9), Col("side", "Side", width=8), Col("quantity", "Quantity", "qty", 10),
        Col("buy_date", "Bought on", "date", 12), Col("sell_date", "Sold on", "date", 12), Col("days_held", "Days held", "int", 9),
        Col("buy_price", "Buy price", "price", 12), Col("sell_price", "Sell price", "price", 12),
        Col("buy_value", "Buy value", "money", 15, True), Col("sell_value", "Sell value", "money", 15, True),
        Col("charges", "Charges", "money", 12, True), Col("gain", "Gain/Loss", "money", 15, True, tone="pnl"),
        Col("source", "From", width=22, get=_src)]
YEARS = [Col("fy", "Financial year", width=12, get=lambda r: "FY " + str(r.get("fy", ""))),
         Col("STCG", "Short-term gains", "money", 16, tone="pnl"), Col("LTCG", "Long-term gains", "money", 16, tone="pnl"),
         Col("Speculative (intraday)", "Intraday (speculative)", "money", 18, tone="pnl"),
         Col("F&O (non-speculative)", "F&O (business)", "money", 16, tone="pnl"),
         Col("fno_turnover", "F&O turnover", "money", 16), Col("charges", "Charges", "money", 14),
         Col("stcg_tax_estimate", "Short-term tax @20% (estimate)", "money", 18),
         Col("ltcg_taxable", "Long-term gains above ₹1.25 lakh", "money", 18),
         Col("ltcg_tax_estimate", "Long-term tax @12.5% (estimate)", "money", 18), Col("trades", "Lots", "int", 8)]

TITLES = {"pnl": "Profit/Loss (closed trades)", "journal": "Order journal", "fills": "Fills", "tax-lots": "Tax lots"}
KINDS = tuple(TITLES)


def _num(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _when(value, kind: str):
    """'2026-11-20 10:15:00' (or ISO with T / offset) to a datetime Excel shows as a date; bad text stays text."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    stamp = stamp.replace(tzinfo=None)            # Excel has no time zones; stored times are already IST
    return stamp.date() if kind == "date" else stamp


def _value(col: Col, row: dict):
    value = col.get(row) if col.get else row.get(col.key)
    if col.kind in ("time", "date"):
        return _when(value, col.kind)
    if col.kind in ("money", "price", "qty", "pct", "int", "ratio"):
        if value in (None, ""):
            return None
        try:
            return float(value) if col.kind != "int" else int(value)
        except (TypeError, ValueError):
            return str(value)
    if value is None or isinstance(value, bool):
        return "" if value is None else ("Yes" if value else "No")
    return value if isinstance(value, (int, float)) else str(value)


def _sheet(ws: Worksheet, title: str, subtitle: str, cols: list[Col], rows: list[dict], *, totals: bool = True,
           empty: str = "Nothing recorded yet on this account.") -> None:
    last_col = get_column_letter(len(cols))
    ws["A1"] = title
    ws["A1"].font = Font(bold=True, size=14, color=NAVY)
    ws["A2"] = subtitle
    ws["A2"].font = Font(italic=True, size=10, color="5B6675")
    ws.sheet_view.showGridLines = False

    head_font, head_fill = Font(bold=True, color=WHITE), PatternFill("solid", fgColor=NAVY)
    for i, col in enumerate(cols, 1):
        cell = ws.cell(HEADER_ROW, i, col.label)
        cell.font, cell.fill = head_font, head_fill
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = Border(bottom=THIN)
        ws.column_dimensions[get_column_letter(i)].width = col.width
    ws.row_dimensions[HEADER_ROW].height = 30

    first = HEADER_ROW + 1
    for r, row in enumerate(rows, first):
        for i, col in enumerate(cols, 1):
            value = _value(col, row)
            cell = ws.cell(r, i, value)
            if isinstance(value, str):
                cell.data_type = "s"             # never a formula, whatever the text starts with
            if col.kind in FORMATS:
                cell.number_format = FORMATS[col.kind]
                cell.alignment = Alignment(horizontal="right")
            else:
                cell.alignment = Alignment(indent=1)
            cell.border = Border(bottom=THIN)
    last = first + len(rows) - 1
    ws.freeze_panes = ws.cell(first, 1)
    ws.print_title_rows = f"{HEADER_ROW}:{HEADER_ROW}"
    ws.page_setup.orientation = "landscape"
    ws.page_setup.fitToWidth, ws.page_setup.fitToHeight = 1, 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    if not rows:
        ws.cell(first, 1, empty).font = Font(italic=True, color="5B6675")
        return
    ws.auto_filter.ref = f"A{HEADER_ROW}:{last_col}{last}"

    # No zebra banding: LibreOffice and Google Sheets apply only the first matching rule, so a band
    # would hide the red/green. Failed-order rows go first so they win over the Buy/Sell colour.
    ordered = sorted(enumerate(cols, 1), key=lambda pair: pair[1].tone != "status")
    for i, col in ordered:
        letter = get_column_letter(i)
        cells = f"{letter}{first}:{letter}{last}"
        if col.tone == "pnl":
            ws.conditional_formatting.add(cells, CellIsRule(operator="greaterThan", formula=["0"], stopIfTrue=True,
                                                            fill=PatternFill("solid", fgColor=GREEN_FILL),
                                                            font=Font(bold=True, color=GREEN_TEXT)))
            ws.conditional_formatting.add(cells, CellIsRule(operator="lessThan", formula=["0"], stopIfTrue=True,
                                                            fill=PatternFill("solid", fgColor=RED_FILL),
                                                            font=Font(bold=True, color=RED_TEXT)))
        elif col.tone == "side":
            for words, color in ((("Buy", "Long"), GREEN_TEXT), (("Sell", "Short"), RED_TEXT)):
                test = "OR(" + ",".join(f'{letter}{first}="{w}"' for w in words) + ")"
                ws.conditional_formatting.add(cells, FormulaRule(formula=[test], font=Font(bold=True, color=color)))
        elif col.tone == "status":
            ws.conditional_formatting.add(f"A{first}:{last_col}{last}",
                                          FormulaRule(formula=[f'${letter}{first}="Failed"'], stopIfTrue=True,
                                                      fill=PatternFill("solid", fgColor=RED_FILL), font=Font(color=RED_TEXT)))
        elif col.tone == "verdict":
            for words, fill, text in ((VERDICT_GOOD, GREEN_FILL, GREEN_TEXT), (VERDICT_WARN, AMBER_FILL, AMBER_TEXT),
                                      (VERDICT_BAD, RED_FILL, RED_TEXT)):
                test = "OR(" + ",".join(f'{letter}{first}="{w}"' for w in words) + ")"
                ws.conditional_formatting.add(cells, FormulaRule(formula=[test], stopIfTrue=True,
                                                                 fill=PatternFill("solid", fgColor=fill), font=Font(bold=True, color=text)))
        elif col.tone == "band":
            for name, color in BAND.items():
                ws.conditional_formatting.add(cells, FormulaRule(formula=[f'{letter}{first}="{name}"'],
                                                                 fill=PatternFill("solid", fgColor=color), font=Font(bold=True)))

    if not totals:
        return
    total_row = last + 2
    top = Border(top=Side(style="medium", color=NAVY), bottom=Side(style="double", color=NAVY))
    ws.cell(total_row, 1, "Total (rows shown)").font = Font(bold=True, color=NAVY)
    for i, col in enumerate(cols, 1):
        cell = ws.cell(total_row, i)
        cell.border = top
        if col.total:
            letter = get_column_letter(i)
            cell.value = f"=SUBTOTAL(109,{letter}{first}:{letter}{last})"
            cell.number_format = FORMATS[col.kind]
            cell.font = Font(bold=True)
            cell.alignment = Alignment(horizontal="right")
            if col.tone == "pnl":
                ref = f"{letter}{total_row}"
                ws.conditional_formatting.add(ref, CellIsRule(operator="greaterThan", formula=["0"],
                                                              fill=PatternFill("solid", fgColor=GREEN_FILL), font=Font(bold=True, color=GREEN_TEXT)))
                ws.conditional_formatting.add(ref, CellIsRule(operator="lessThan", formula=["0"],
                                                              fill=PatternFill("solid", fgColor=RED_FILL), font=Font(bold=True, color=RED_TEXT)))
    ws.cell(total_row + 1, 1, f"=SUBTOTAL(103,A{first}:A{last})&\" of {len(rows)} rows\"").font = Font(italic=True, color="5B6675")


def _pnl_summary(ws: Worksheet, rows: list[dict]) -> None:
    """Wins, losses and the net for each source (algo or manual): a short second sheet."""
    groups: dict[str, dict] = {}
    for row in rows:
        g = groups.setdefault(source_label(row.get("source")), {"source": source_label(row.get("source")), "trades": 0,
                                                                 "wins": 0, "losses": 0, "won": 0.0, "lost": 0.0, "net": 0.0})
        pnl = _num(row.get("pnl"))
        g["trades"] += 1
        g["net"] += pnl
        if pnl > 0:
            g["wins"] += 1
            g["won"] += pnl
        elif pnl < 0:
            g["losses"] += 1
            g["lost"] += pnl
    out = sorted(groups.values(), key=lambda g: g["net"], reverse=True)
    for g in out:
        g["win_rate"] = round(g["wins"] / g["trades"] * 100, 2) if g["trades"] else 0.0
    cols = [Col("source", "From", width=26), Col("trades", "Trades", "int", 9, True), Col("wins", "Profitable", "int", 11, True),
            Col("losses", "Loss-making", "int", 12, True), Col("win_rate", "Win rate", "pct", 10),
            Col("won", "Total profit", "money", 16, True, tone="pnl"), Col("lost", "Total loss", "money", 16, True, tone="pnl"),
            Col("net", "Net Profit/Loss", "money", 16, True, tone="pnl")]
    _sheet(ws, "Profit/Loss by source", "Closed trades only, after charges. Algos and manual trades.", cols, out)


def _stamp() -> str:
    return datetime.now().strftime("%d %b %Y, %H:%M")


def workbook(kind: str, rows: list[dict], mode: str, *, fy: str = "", years: list[dict] | None = None,
             app_name: str = "TradeSetu") -> bytes:
    """One export as an .xlsx file. ``rows`` are the same dicts the CSV export writes."""
    if kind not in TITLES:
        raise KeyError(kind)
    account = "Paper account" if mode == "paper" else "Live account"
    subtitle = f"{app_name} · {account}{' · FY ' + fy if fy else ''} · {len(rows)} rows · made {_stamp()} IST"
    wb = Workbook()
    ws = wb.active
    if kind == "pnl":
        ws.title = "Closed trades"
        _sheet(ws, TITLES[kind], subtitle + " · after charges", PNL, rows, empty="No closed trades yet on this account.")
        _pnl_summary(wb.create_sheet("By source"), rows)
    elif kind == "journal":
        ws.title = "Order journal"
        _sheet(ws, TITLES[kind], subtitle + " · failed orders in red", JOURNAL, rows, totals=False,
               empty="No orders yet on this account.")
    elif kind == "fills":
        ws.title = "Fills"
        _sheet(ws, TITLES[kind], subtitle, FILLS, rows, empty="No fills yet on this account.")
    else:
        ws.title = "Tax lots"
        _sheet(ws, TITLES[kind], subtitle + " · sales matched first in, first out", LOTS, rows,
               empty="No closed tax lots yet on this account.")
        summary = wb.create_sheet("By financial year")
        _sheet(summary, "Tax estimate by financial year",
               "Indicative only: rates from 23 July 2024; check with your broker's tax P&L and a chartered accountant.",
               YEARS, years if years is not None else tax.summary(rows), totals=False)
    wb.properties.creator = app_name
    wb.properties.title = TITLES[kind]
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


# -- AI strategy scan ------------------------------------------------------------------------------------

def _get(*path):
    def read(row):
        for key in path:
            row = row.get(key) if isinstance(row, dict) else None
        return row
    return read


SCAN_RANKING = [
    Col("rank", "Rank", "int", 6), Col("verdict", "Verdict", width=20, tone="verdict"), Col("name", "Strategy", width=34),
    Col("settings", "Settings (chosen in-sample)", width=36), Col("exits_text", "Exits", width=22),
    Col("oos_return", "Unseen return", "pct", 11, get=_get("unseen", "return_pct"), tone="pnl"),
    Col("oos_cagr", "Unseen return a year", "pct", 11, get=_get("unseen", "cagr_pct"), tone="pnl"),
    Col("vs_buy_hold_pct", "Vs buy and hold, a year", "pct", 12, tone="pnl"),
    Col("oos_sharpe", "Unseen Sharpe ratio", "ratio", 10, get=_get("unseen", "sharpe"), tone="pnl"),
    Col("oos_dd", "Unseen worst fall", "pct", 10, get=_get("unseen", "max_drawdown_pct")),
    Col("luck", "Chance it is not luck", "pct", 11, get=lambda r: None if r.get("luck_test") is None else r["luck_test"] * 100),
    Col("robust", "Neighbouring settings that also made money", "pct", 14,
        get=lambda r: None if r.get("robust") is None else r["robust"] * 100),
    Col("stocks_profitable", "Stocks in profit (unseen)", "int", 10), Col("stocks", "Stocks", "int", 8),
    Col("oos_trades", "Unseen trades", "int", 9), Col("is_trades", "In-sample trades", "int", 10),
    Col("is_return", "In-sample return", "pct", 11, get=_get("in_sample", "return_pct"), tone="pnl"),
    Col("is_sharpe", "In-sample Sharpe ratio", "ratio", 10, get=_get("in_sample", "sharpe")),
    Col("tried", "Settings tried", "int", 9), Col("charges", "Charges paid", "money", 14),
]
SCAN_STOCKS = [
    Col("symbol", "Stock", width=14), Col("name", "Company", width=30),
    Col("best", "Best fit for this stock (in-sample)", width=34), Col("best_settings", "Its settings", width=34),
    Col("best_is_return_pct", "Its in-sample return", "pct", 12, tone="pnl"),
    Col("best_oos_return_pct", "Its unseen return", "pct", 12, tone="pnl"),
    Col("winner_oos_return_pct", "Winner's unseen return", "pct", 12, tone="pnl"),
    Col("bh_oos_return_pct", "Buy and hold, unseen", "pct", 12, tone="pnl"),
]
SCAN_EXITS = [
    Col("strategy", "Strategy", width=34), Col("settings", "Settings", width=34), Col("exits", "Exits", width=24),
    Col("chosen", "Chosen (in-sample)", width=10, tone="verdict"),
    Col("is_return_pct", "In-sample return", "pct", 12, tone="pnl"), Col("is_sharpe", "In-sample Sharpe ratio", "ratio", 11),
    Col("oos_return_pct", "Unseen return", "pct", 12, tone="pnl"), Col("oos_sharpe", "Unseen Sharpe ratio", "ratio", 11, tone="pnl"),
    Col("oos_max_drawdown_pct", "Unseen worst fall", "pct", 11), Col("oos_trades", "Unseen trades", "int", 10),
]
SCAN_RUNS = [
    Col("symbol", "Stock", width=13), Col("strategy", "Strategy", width=32), Col("settings", "Settings", width=34),
    Col("exits", "Exits", width=20),
    Col("is_return_pct", "In-sample return", "pct", 11, tone="pnl"), Col("is_sharpe", "In-sample Sharpe ratio", "ratio", 10),
    Col("is_trades", "In-sample trades", "int", 9),
    Col("oos_return_pct", "Unseen return", "pct", 11, tone="pnl"), Col("oos_sharpe", "Unseen Sharpe ratio", "ratio", 10, tone="pnl"),
    Col("oos_max_drawdown_pct", "Unseen worst fall", "pct", 10), Col("oos_trades", "Unseen trades", "int", 9),
    Col("return_pct", "Whole-period return", "pct", 11, tone="pnl"), Col("max_drawdown_pct", "Whole-period worst fall", "pct", 10),
    Col("trades", "Trades", "int", 8), Col("win_rate_pct", "Win rate", "pct", 9), Col("charges", "Charges", "money", 12, True),
]
SCAN_ROWS_MAX = 250_000


def scan_workbook(r: dict, app_name: str = "TradeSetu") -> bytes:
    """The AI strategy scan as a workbook: the ranking, the method and caveats, each stock, the stoploss and
    target trials, and every backtest run."""
    period = f"{r['start']} to {r['end']}"
    head = f"{app_name} · {r['index'] or 'Index'} · {r['stocks_tested']} stocks · {period} · made {_stamp()} IST"
    wb = Workbook()
    ws = wb.active
    ws.title = "Summary"
    w = r.get("winner")
    winner = (f"Winner: {w['name']} ({w['settings']}; {w['exits_text'].lower()}) · {w['verdict']}" if w
              else "No strategy passed every check: see the Read me sheet")
    _sheet(ws, f"AI strategy scan: {r['index'] or 'index'}", f"{head} · {winner}", SCAN_RANKING, r["ranking"], totals=False)
    bh = r["buy_hold"]["unseen"]
    note_row = HEADER_ROW + len(r["ranking"]) + 2
    ws.cell(note_row, 1, f"Buy and hold of the same stocks, unseen part: return {bh['return_pct']}%, "
                         f"{bh['cagr_pct']}% a year, Sharpe ratio {bh['sharpe']}, worst fall {bh['max_drawdown_pct']}%. "
                         f"Ranked on unseen results only ({r['period']['unseen'][0]} to {r['period']['unseen'][1]}).").font = \
        Font(italic=True, color="5B6675")

    about = wb.create_sheet("Read me")
    facts = [
        ("Result", r["explanation"]),
        ("Index", r["index"]), ("Stocks tested", f"{r['stocks_tested']} of {r['stocks_asked']}"),
        ("Period", period), ("Settings chosen on (in-sample)", " to ".join(r["period"]["in_sample"])),
        ("Strategies judged on (unseen)", " to ".join(r["period"]["unseen"]) + f" ({r['oos_pct']:g}% of the period)"),
        ("Search", r["depth_text"]), ("Settings tried", f"{r['settings_tried']} across {r['strategies_compared']} strategies"),
        ("Backtests run", f"{r['trials']:,}"),
        ("Luck test", f"Deflated Sharpe ratio counting {r['luck_trials']} choices made with the unseen results; "
                      f"a strategy passes at 95%."),
        ("Capital", f"₹{r['capital']:,.0f}, split equally: ₹{r['money_per_stock']:,.0f} a stock"),
        ("Charges", f"Delivery (CNC) brokerage, STT, stamp duty, exchange and SEBI fees, GST and DP charges on every fill, "
                    f"plus {r['slippage_bps']:g} basis points of slippage"),
        ("Prices from", ", ".join(f"{k}: {v} stocks" for k, v in r["sources"].items()) or "–"),
        ("How it trades", "Day candles. A signal on a day's close is traded at the next day's open. Long only, whole "
                          "shares, one position per stock."),
    ] + [(f"Verdict: {name}", meaning) for name, (_, meaning) in _verdicts().items()]
    _sheet(about, "How to read this scan", head, [Col("item", "Item", width=30), Col("value", "Details", width=120)],
           [{"item": k, "value": v} for k, v in facts], totals=False)
    for row in about.iter_rows(min_row=HEADER_ROW + 1):
        row[1].alignment = Alignment(wrap_text=True, vertical="top", indent=1)
        row[0].alignment = Alignment(vertical="top", indent=1)
        row[0].font = Font(bold=True)

    _sheet(wb.create_sheet("Best per stock"), "Each stock: its own best fit against the one winner",
           "A best fit chosen for one stock usually does worse on unseen data than one strategy that works on many: "
           "compare the two unseen columns.", SCAN_STOCKS, r["per_stock"], totals=False)
    _sheet(wb.create_sheet("Stoploss and target"), "Stoploss and target levels, chosen in-sample",
           "Tried on the three strategies with the best unseen Sharpe ratio. Each line runs on every stock.",
           SCAN_EXITS, r["exit_trials"], totals=False, empty="Not tried (the scan was stopped early).")
    runs = r["runs"][:SCAN_ROWS_MAX]
    extra = f" · first {SCAN_ROWS_MAX:,} of {len(r['runs']):,}" if len(r["runs"]) > SCAN_ROWS_MAX else ""
    _sheet(wb.create_sheet("All runs"), "Every backtest run", f"{head}{extra} · one row per stock, strategy and setting",
           SCAN_RUNS, runs)
    skipped = [{"symbol": s["symbol"].removesuffix(".NS"), "reason": s["reason"]} for s in r["skipped"]]
    _sheet(wb.create_sheet("Notes"), "Stocks left out", "Not in any result above.",
           [Col("symbol", "Stock", width=16), Col("reason", "Why", width=100)], skipped, totals=False,
           empty="None: every stock was tested.")
    wb.properties.creator = app_name
    wb.properties.title = "AI strategy scan"
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def _verdicts() -> dict:
    from .aiscan import VERDICTS
    return VERDICTS


__all__ = ["KINDS", "TITLES", "workbook", "scan_workbook", "source_label"]
