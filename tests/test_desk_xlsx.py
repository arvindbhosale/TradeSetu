"""Excel exports: formats, colours and that no cell text runs as a formula."""

import io

import pytest

openpyxl = pytest.importorskip("openpyxl")

from desk import xlsxexport as x  # noqa: E402


def _book(kind, rows, **kw):
    return openpyxl.load_workbook(io.BytesIO(x.workbook(kind, rows, "paper", **kw)))


CLOSED = [{"time_ist": "2026-11-20 10:15:00", "source": "algo:Breakout", "symbol": "TCS", "side": "LONG", "quantity": 2,
           "buy_price": 4000, "sell_price": 4100, "charges": 19.5, "pnl": 180.5, "return_pct": 2.25},
          {"time_ist": "2026-11-20 14:00:00", "source": "manual", "symbol": "INFY", "side": "SHORT", "quantity": 5,
           "buy_price": 1500, "sell_price": 1490, "charges": 12.0, "pnl": -62.0, "return_pct": -0.83}]


def test_closed_trades_sheet_has_types_totals_and_colours():
    book = _book("pnl", CLOSED)
    ws = book["Closed trades"]
    assert ws["A1"].value == "Profit/Loss (closed trades)" and "Paper account" in ws["A2"].value
    assert ws["A5"].is_date and ws["A5"].number_format == x.TIME
    assert ws["B5"].value == "Algo: Breakout" and ws["D6"].value == "Short"
    assert ws["H4"].value == "Charges" and ws["I5"].value == 180.5 and "₹" in ws["I5"].number_format
    assert ws["I8"].value == "=SUBTOTAL(109,I5:I6)" and ws["A8"].value == "Total (rows shown)"
    assert ws.freeze_panes == "A5" and ws.auto_filter.ref == "A4:J6"
    ranges = {str(r.sqref) for r in ws.conditional_formatting}
    assert "I5:I6" in ranges and "D5:D6" in ranges
    rules = [rule for cf in ws.conditional_formatting if str(cf.sqref) == "I5:I6" for rule in cf.rules]
    assert {r.operator for r in rules} == {"greaterThan", "lessThan"}
    by = book["By source"]
    assert [by.cell(r, 1).value for r in (5, 6)] == ["Algo: Breakout", "Manual"] and by["H5"].value == 180.5


def test_text_that_looks_like_a_formula_stays_text():
    rows = [{"time_ist": "2026-11-20 10:15:00", "mode": "paper", "source": "manual", "exchange": "NSE",
             "tradingsymbol": "=HYPERLINK(\"http://x\")", "side": "BUY", "quantity": 1, "ok": False, "error": "+cmd|' /C calc'!A0"}]
    ws = _book("journal", rows)["Order journal"]
    assert ws["E5"].data_type == "s" and ws["E5"].value.startswith("=HYPERLINK")
    assert ws["O5"].data_type == "s" and ws["N5"].value == "Failed"
    book = openpyxl.load_workbook(io.BytesIO(x.workbook("journal", rows, "paper")))
    assert book["Order journal"]["E5"].data_type == "s"


def test_empty_account_and_tax_lots_summary():
    ws = _book("fills", [])["Fills"]
    assert ws["A5"].value == "No fills yet on this account." and ws.auto_filter.ref is None
    lots = [{"fy": "2026-27", "category": "STCG", "symbol": "TCS", "exchange": "NSE", "side": "Long", "quantity": 2, "buy_date": "2026-05-01",
             "sell_date": "2026-11-20", "days_held": 203, "buy_price": 4000, "sell_price": 4100, "buy_value": 8000,
             "sell_value": 8200, "charges": 20, "gain": 180, "source": "manual"}]
    book = _book("tax-lots", lots, fy="2026-27")
    ws = book["Tax lots"]
    assert ws["A5"].value == "FY 2026-27" and ws["E5"].value == "Long" and ws["G5"].is_date and ws["G5"].number_format == x.DATE
    assert ws["O7"].value == "=SUBTOTAL(109,O5:O5)" and "FY 2026-27" in ws["A2"].value
    assert book["By financial year"]["B5"].value == 180


def test_journal_status_says_filled_set_placed_or_failed():
    base = {"time_ist": "2026-11-20 10:15:00", "mode": "paper", "source": "manual", "exchange": "NSE",
            "tradingsymbol": "TCS", "side": "BUY", "quantity": 1}
    rows = [{**base, "ok": True, "fill_price": 4000.0}, {**base, "ok": True, "gtt": True},
            {**base, "ok": True}, {**base, "ok": False, "error": "no cash"}]
    ws = _book("journal", rows)["Order journal"]
    assert [ws.cell(r, 14).value for r in range(5, 9)] == ["Filled", "Set", "Placed", "Failed"]


def test_unknown_kind():
    with pytest.raises(KeyError):
        x.workbook("other", [], "paper")
